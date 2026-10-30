"""Area import: tiles covering a place are cut from a regional OpenStreetMap
extract plus elevation tiles and cached, so a network graph can be built
without further HTTP calls.

An area is a lens onto the shared tile cache (map_tiles on tiles.py's fixed
grid): overlapping areas share tiles. An area's own row (center, radius,
label, pinned) is athlete-authored; tile membership and import status belong
to the import job and are safe to recompute.

Map data refreshes only on request (import_area with refresh=True). An
unpinned area untouched for UNPINNED_GC_DAYS is deleted on the next
import_area call, along with any tile no area references.
"""

import logging
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, cast
from typing_extensions import NotRequired, TypedDict

from . import db, elevation, extracts, familiarity, geocode, geofabrik, metrics, network, sections, tilecache, tiles
from .errors import describe_error

_logger = logging.getLogger(__name__)

# Rough throughput of a filtered pass over an extract; estimates only.
EXTRACT_CUT_MB_PER_S = 13.7

DRIVE_KM_PER_MINUTE = 1.0
# Largest import radius (explicit or from drive minutes): a bigger area is a
# much bigger cut, elevation prefetch and graph than a search needs.
MAX_RADIUS_KM = 60.0

REFRESH_SUGGESTED_AFTER_DAYS = 180.0
UNPINNED_GC_DAYS = 60.0


_import_lock = threading.Lock()


class AreaNotFoundError(Exception):
    pass


class AreaImportError(Exception):
    pass


class AreaSummary(TypedDict):
    area_id: int
    label: str
    center_lat: float
    center_lng: float
    radius_km: float
    pinned: bool
    status: str
    created_at: str
    last_used_at: str
    imported_at: str | None
    phase: str | None
    done: int | None
    total: int | None
    error: str | None
    data_as_of: NotRequired[str | None]


class AreaDetail(AreaSummary):
    drive_minutes: float | None
    tile_count: int
    region: NotRequired[str | None]
    data_as_of: str | None
    refresh_suggested: bool


class ImportEstimate(TypedDict):
    # "extract:<region>" for a registered file, "pending-extract:<region>" for
    # one still to download (regions comma-separated), "pending-extract" when
    # the region is unknown without fetching the index.
    source: str
    tile_count: int
    # None while the extract still has to be downloaded.
    estimated_seconds: float | None


class AreaStatus(TypedDict):
    area_id: int
    label: str
    status: str  # "new" | "importing" | "ready" | "error"
    phase: NotRequired[str | None]
    done: NotRequired[int | None]
    total: NotRequired[int | None]
    error: NotRequired[str | None]
    estimate: NotRequired[ImportEstimate]
    # Present when the center was geocoded: display name and licence attribution.
    geocode: NotRequired[geocode.GeocodeResult]
    data_as_of: NotRequired[str | None]
    refresh_suggested: NotRequired[bool]


def init_area_schema(conn: sqlite3.Connection) -> None:
    # source is "extract:<region>". inputs is a JSON object of region -> fetched_at of the extract
    # files a tile was cut from, NULL for uploaded or older tiles.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS map_tiles (
            gx            INTEGER NOT NULL,
            gy            INTEGER NOT NULL,
            query_version INTEGER NOT NULL,
            fetched_at    TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            data          BLOB NOT NULL,
            source        TEXT NOT NULL,
            inputs        TEXT,
            PRIMARY KEY (gx, gy)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS elevation_tiles (
            zoom       INTEGER NOT NULL,
            x          INTEGER NOT NULL,
            y          INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            data       BLOB NOT NULL,
            PRIMARY KEY (zoom, x, y)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            query        TEXT PRIMARY KEY,
            lat          REAL NOT NULL,
            lng          REAL NOT NULL,
            display_name TEXT,
            fetched_at   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_misses (
            query      TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reverse_geocode_cache (
            point      TEXT PRIMARY KEY,
            label      TEXT,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS areas (
            area_id            INTEGER PRIMARY KEY,
            label              TEXT NOT NULL,
            center_lat         REAL NOT NULL,
            center_lng         REAL NOT NULL,
            radius_km          REAL NOT NULL,
            drive_minutes      REAL,
            pinned             INTEGER NOT NULL DEFAULT 0,
            status             TEXT NOT NULL DEFAULT 'new',
            status_phase       TEXT,
            status_done        INTEGER,
            status_total       INTEGER,
            status_error       TEXT,
            created_at         TEXT NOT NULL,
            last_used_at       TEXT NOT NULL,
            imported_at        TEXT,
            region             TEXT
        )
    """)
    # No FK on (gx, gy) -> map_tiles: a row here is written before the tile
    # fetch that creates its map_tiles row completes (see _do_import), and a
    # tile can also be evicted by gc_tiles once no area references it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS area_tiles (
            area_id INTEGER NOT NULL REFERENCES areas(area_id),
            gx      INTEGER NOT NULL,
            gy      INTEGER NOT NULL,
            PRIMARY KEY (area_id, gx, gy)
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Row <-> dict helpers
# ---------------------------------------------------------------------------


def _get_area_row(conn: sqlite3.Connection, area_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM areas WHERE area_id = ?", (area_id,)).fetchone()
    if row is None:
        raise AreaNotFoundError(f"no area {area_id}")
    return row


_SUMMARY_COLUMNS = (
    "area_id", "label", "center_lat", "center_lng", "radius_km", "status", "created_at", "last_used_at", "imported_at",
)


def _row_to_summary(row: sqlite3.Row) -> AreaSummary:
    return cast(AreaSummary, {
        **{c: row[c] for c in _SUMMARY_COLUMNS},
        "pinned": bool(row["pinned"]),
        "phase": row["status_phase"],
        "done": row["status_done"],
        "total": row["status_total"],
        "error": row["status_error"],
    })


def _row_to_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> AreaDetail:
    tile_count = conn.execute(
        "SELECT COUNT(*) FROM area_tiles WHERE area_id = ?", (row["area_id"],)
    ).fetchone()[0]
    summary = _row_to_summary(row)
    data_as_of, refresh_suggested = area_freshness(conn, row["area_id"])
    return {
        **summary,
        "drive_minutes": row["drive_minutes"],
        "tile_count": tile_count,
        "region": row["region"],
        "data_as_of": data_as_of,
        "refresh_suggested": refresh_suggested,
    }


def area_freshness(conn: sqlite3.Connection, area_id: int) -> tuple[str | None, bool]:
    """(data_as_of, refresh_suggested): the oldest date any tile's data is as
    of (the extracts' own dates, or the cut time for an uploaded cut) and
    whether it is older than REFRESH_SUGGESTED_AFTER_DAYS. (None, False) before
    any tile is cached."""
    rows = conn.execute(
        """
        SELECT mt.source, MIN(mt.fetched_at) AS fetched_at
        FROM area_tiles atl
        JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ?
        GROUP BY mt.source
        """,
        (area_id,),
    ).fetchall()
    dates: list[str] = []
    for row in rows:
        regions = extracts.regions_of_source(row["source"])
        records = [extracts.get_extract(conn, r) for r in regions]
        dates.append(min([r["data_as_of"] for r in records if r is not None], default=row["fetched_at"]))
    as_of = min(dates, default=None)
    if as_of is None:
        return None, False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(as_of)
    return as_of, age > timedelta(days=REFRESH_SUGGESTED_AFTER_DAYS)


def _row_to_status(row: sqlite3.Row) -> AreaStatus:
    return {
        "area_id": row["area_id"],
        "label": row["label"],
        "status": row["status"],
        "phase": row["status_phase"],
        "done": row["status_done"],
        "total": row["status_total"],
        "error": row["status_error"],
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def get_area(conn: sqlite3.Connection, area_id: int) -> AreaDetail:
    return _row_to_detail(conn, _get_area_row(conn, area_id))


def rebuild_stale_network(conn: sqlite3.Connection, area_id: int) -> bool:
    """Rebuild an area's network from cached tiles when built by an older
    network.NETWORK_DERIVE_VERSION (no extract fetch). Returns True while the
    stale graph is being replaced; callers must not search or treat it as ready."""
    if not network.graph_is_stale(conn, area_id):
        return False
    if _try_start_import(conn, area_id):
        threading.Thread(target=_run_finish_only_job, args=(area_id,), daemon=True).start()
    return True


def get_area_status(conn: sqlite3.Connection, area_id: int) -> AreaStatus:
    _get_area_row(conn, area_id)
    rebuild_stale_network(conn, area_id)
    status = _row_to_status(_get_area_row(conn, area_id))
    status["data_as_of"], status["refresh_suggested"] = area_freshness(conn, area_id)
    return status


def list_areas(conn: sqlite3.Connection) -> list[AreaSummary]:
    rows = conn.execute("SELECT * FROM areas ORDER BY last_used_at DESC").fetchall()
    return [{**_row_to_summary(r), "data_as_of": area_freshness(conn, r["area_id"])[0]} for r in rows]


def set_pinned(conn: sqlite3.Connection, area_id: int, pinned: bool) -> AreaDetail:
    _get_area_row(conn, area_id)
    conn.execute("UPDATE areas SET pinned = ? WHERE area_id = ?", (int(pinned), area_id))
    conn.commit()
    return get_area(conn, area_id)


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------


def gc_areas_and_tiles(conn: sqlite3.Connection) -> None:
    """Delete unpinned areas untouched for UNPINNED_GC_DAYS and any map tile
    no remaining area references. Runs at the start of import_area; there is
    no scheduler."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=UNPINNED_GC_DAYS)).isoformat()
    stale_ids = [
        r["area_id"]
        for r in conn.execute(
            "SELECT area_id FROM areas WHERE pinned = 0 AND last_used_at < ?", (cutoff,)
        ).fetchall()
    ]
    for area_id in stale_ids:
        delete_area(conn, area_id)
    _drop_orphan_tiles(conn)


def delete_area(conn: sqlite3.Connection, area_id: int) -> None:
    """Delete an area's rows (tiles, network, sections). Shared tiles stay
    until _drop_orphan_tiles."""
    conn.execute("DELETE FROM area_tiles WHERE area_id = ?", (area_id,))
    conn.execute("DELETE FROM net_edges WHERE area_id = ?", (area_id,))
    conn.execute("DELETE FROM net_nodes WHERE area_id = ?", (area_id,))
    conn.execute("DELETE FROM net_graphs WHERE area_id = ?", (area_id,))
    sections.delete_area_sections(conn, area_id)
    conn.execute("DELETE FROM areas WHERE area_id = ?", (area_id,))
    conn.commit()


def _drop_orphan_tiles(conn: sqlite3.Connection) -> None:
    conn.execute("""
        DELETE FROM map_tiles
        WHERE NOT EXISTS (
            SELECT 1 FROM area_tiles
            WHERE area_tiles.gx = map_tiles.gx AND area_tiles.gy = map_tiles.gy
        )
    """)
    conn.commit()


def remove_area(conn: sqlite3.Connection, area_id: int) -> list[str]:
    """Delete an idle area, its orphaned tiles, and every downloaded regional
    extract no remaining area would be cut from. Returns the regions freed.

    An extract is needed when extracts.planned_regions names it for a remaining
    area. If any remaining area has no stored plan the answer is unknown and
    nothing is freed. Extracts registered from a local file are never deleted."""
    if _get_area_row(conn, area_id)["status"] == "importing":
        raise AreaImportError(f"area {area_id} is importing; try again when it finishes")
    delete_area(conn, area_id)
    _drop_orphan_tiles(conn)

    needed: set[str] = set()
    for area in conn.execute("SELECT * FROM areas").fetchall():
        bbox = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])
        planned = extracts.planned_regions(conn, bbox, area["region"])
        if not planned:
            return []
        needed.update(planned)
    unused = [r for r in extracts.list_extracts(conn)
              if r["region"] not in needed and not r["region"].startswith(extracts.LOCAL_REGION_PREFIX)]
    for record in unused:
        Path(record["path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM map_extracts WHERE region = ?", (record["region"],))
    conn.commit()
    return [r["region"] for r in unused]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _resolve_radius_km(radius_km: float | None, drive_minutes: float | None) -> float:
    if radius_km is not None:
        if not 0 < radius_km <= MAX_RADIUS_KM:
            raise ValueError(f"radius_km must be above 0 and at most {MAX_RADIUS_KM:g}")
        return radius_km
    if drive_minutes is not None:
        # A flat stand-in for a drive-time isochrone.
        return min(drive_minutes * DRIVE_KM_PER_MINUTE, MAX_RADIUS_KM)
    raise ValueError("import_area needs radius_km or drive_minutes")


def _resolve_center(
    conn: sqlite3.Connection, place: str | None, lat: float | None, lng: float | None, refresh_geocode: bool = False
) -> tuple[float, float, geocode.GeocodeResult | None]:
    """(lat, lng, geocode result when the center came from a place name)."""
    if place is not None:
        try:
            found = geocode.lookup(conn, place, refresh=refresh_geocode)
        except geocode.GeocodeError as e:
            raise AreaImportError(str(e)) from e
        return (found["lat"], found["lng"], found)
    if lat is not None and lng is not None:
        return (lat, lng, None)
    raise ValueError("import_area needs place, or lat and lng")


def _try_start_import(conn: sqlite3.Connection, area_id: int) -> bool:
    """Atomically flip an area to 'importing' unless an import holds it;
    SQLite serializes writers, so racing callers cannot both win."""
    cur = conn.execute(
        """
        UPDATE areas SET status = 'importing', status_phase = NULL,
            status_done = NULL, status_total = NULL, status_error = NULL
        WHERE area_id = ? AND status != 'importing'
        """,
        (area_id,),
    )
    conn.commit()
    return cur.rowcount > 0



def _upsert_area(
    conn: sqlite3.Connection,
    label: str,
    center_lat: float,
    center_lng: float,
    radius_km: float,
    drive_minutes: float | None,
    pinned: bool | None,
    region: str | None,
    now: str,
) -> int:
    """The area_id of label's row, updated to the given center and size (pinned
    and region are kept when None) or inserted as 'new'."""
    existing = conn.execute("SELECT area_id FROM areas WHERE label = ?", (label,)).fetchone()
    if existing is not None:
        area_id = int(existing["area_id"])
        conn.execute(
            """
            UPDATE areas SET center_lat = ?, center_lng = ?, radius_km = ?, drive_minutes = ?,
                pinned = COALESCE(?, pinned), region = COALESCE(?, region), last_used_at = ?
            WHERE area_id = ?
            """,
            (
                center_lat, center_lng, radius_km, drive_minutes,
                None if pinned is None else int(pinned), region, now, area_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO areas (
                label, center_lat, center_lng, radius_km, drive_minutes,
                pinned, region, status, created_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (label, center_lat, center_lng, radius_km, drive_minutes, int(bool(pinned)), region, now, now),
        )
        area_id = int(cur.lastrowid)  # type: ignore[arg-type]
    conn.commit()
    return area_id


def _start_job_or_touch(conn: sqlite3.Connection, area_id: int, now: str, target: Callable[..., None], *args: object) -> None:
    """Start target(area_id, *args) in a thread unless an import already holds
    the area, in which case only last_used_at moves."""
    if _try_start_import(conn, area_id):
        threading.Thread(target=target, args=(area_id, *args), daemon=True).start()
    else:
        conn.execute("UPDATE areas SET last_used_at = ? WHERE area_id = ?", (now, area_id))
        conn.commit()


def _estimate_import(conn: sqlite3.Connection, bbox: tuple[float, float, float, float], region: str | None) -> ImportEstimate:
    """Source, tile count, and expected seconds, from what is already
    registered and cached; never downloads or fetches the index."""
    needed = tiles.tiles_in_bbox(*bbox)
    planned = extracts.planned_regions(conn, bbox, region)
    records = [extracts.get_extract(conn, path) for path in planned]
    if records and all(r is not None and os.path.isfile(r["path"]) for r in records):
        size_mb = sum(os.path.getsize(r["path"]) for r in records if r is not None) / (1024 * 1024)
        return {
            "source": extracts.source_of_regions(planned),
            "tile_count": len(needed),
            "estimated_seconds": max(5.0, size_mb / EXTRACT_CUT_MB_PER_S),
        }
    chosen = ",".join(planned)
    return {
        "source": f"pending-extract:{chosen}" if chosen else "pending-extract",
        "tile_count": len(needed),
        "estimated_seconds": None,
    }


def estimate_for(conn: sqlite3.Connection, lat: float, lng: float, radius_km: float) -> ImportEstimate:
    """What an import of this circle would use and cost, before committing to it."""
    bbox = tiles.bbox_for_center_radius(lat, lng, _resolve_radius_km(radius_km, None))
    return _estimate_import(conn, bbox, None)


def import_area(
    conn: sqlite3.Connection,
    *,
    place: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    drive_minutes: float | None = None,
    label: str,
    pinned: bool | None = None,
    region: str | list[str] | None = None,
    refresh: bool = False,
    refresh_geocode: bool = False,
) -> AreaStatus:
    """Create (or reuse, keyed by label) an area and start a background
    import unless one is running; returns the status plus an import estimate.

    region is a Geofabrik path or several (list or comma-separated), used only
    when no registered extract covers the bbox; otherwise the cheapest covering
    set is chosen. An unlisted path is refused (at once if the index was
    fetched before, else by the background job). pinned and region are kept
    when None, as are the stored center and radius when no place, coordinates,
    or size is given for an existing label.

    Cached map data is reused; refresh=True re-downloads an extract Geofabrik
    has a newer version of. refresh_geocode=True re-looks-up `place`."""
    if not label:
        raise ValueError("import_area needs a label")
    region_paths = extracts.split_regions(region)
    region = ",".join(region_paths) or None
    known = geofabrik.cached_regions(conn) if region_paths else None
    if known is not None:
        for path in region_paths:
            if extracts.get_extract(conn, path) is None:
                try:
                    geofabrik.check_region(known, path)
                except geofabrik.RegionError as e:
                    raise ValueError(str(e)) from e
    gc_areas_and_tiles(conn)

    existing = conn.execute("SELECT * FROM areas WHERE label = ?", (label,)).fetchone()
    geocoded: geocode.GeocodeResult | None = None
    if (
        existing is not None
        and place is None and lat is None and lng is None and radius_km is None and drive_minutes is None
    ):
        center_lat, center_lng = existing["center_lat"], existing["center_lng"]
        resolved_radius, drive_minutes = existing["radius_km"], existing["drive_minutes"]
    else:
        center_lat, center_lng, geocoded = _resolve_center(conn, place, lat, lng, refresh_geocode)
        resolved_radius = _resolve_radius_km(radius_km, drive_minutes)
    now = datetime.now(timezone.utc).isoformat()

    area_id = _upsert_area(conn, label, center_lat, center_lng, resolved_radius, drive_minutes, pinned, region, now)

    bbox = tiles.bbox_for_center_radius(center_lat, center_lng, resolved_radius)
    estimate = _estimate_import(conn, bbox, region)

    _start_job_or_touch(conn, area_id, now, _run_import_job, refresh)
    status = get_area_status(conn, area_id)
    status["estimate"] = estimate
    if geocoded is not None:
        status["geocode"] = geocoded
    return status


def refresh_area(conn: sqlite3.Connection, label: str, *, force_extract: bool = False) -> AreaStatus:
    """Re-fetch an existing area's map data in this thread and return its
    final status (the CLI path). force_extract replaces the regional extract
    unconditionally, for a bad file that is still registered."""
    row = conn.execute("SELECT area_id FROM areas WHERE label = ?", (label,)).fetchone()
    if row is None:
        raise AreaNotFoundError(f"no area labeled {label!r}")
    area_id = int(row["area_id"])
    if not _try_start_import(conn, area_id):
        raise AreaImportError(f"area {label!r} is already importing")
    _run_import_job(area_id, True, force_extract)
    return get_area_status(conn, area_id)


def _set_progress(conn: sqlite3.Connection, area_id: int, *, phase: str, done: int, total: int | None) -> None:
    conn.execute(
        "UPDATE areas SET status_phase = ?, status_done = ?, status_total = ? WHERE area_id = ?",
        (phase, done, total, area_id),
    )
    conn.commit()


def select_area_extracts(
    conn: sqlite3.Connection, area_id: int, refresh: bool = False, force_extract: bool = False
) -> list[extracts.ExtractRecord]:
    """The extracts to cut area_id from (extracts.select_extracts), downloading
    missing ones. Phase reads "waiting-for-download" while another import holds
    the download, "downloading" (bytes received; total None if undeclared)
    while this one does."""
    area = _get_area_row(conn, area_id)
    bbox = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])

    def downloading(written: int, expected: int | None) -> None:
        _set_progress(conn, area_id, phase="downloading", done=written, total=expected)

    return extracts.select_extracts(
        conn,
        bbox,
        area["region"],
        refresh=refresh,
        force=force_extract,
        on_wait=lambda: _set_progress(conn, area_id, phase="waiting-for-download", done=0, total=1),
        on_progress=downloading,
    )


def _fetch_tiles_phase(
    conn: sqlite3.Connection, area_id: int, area: sqlite3.Row, selected: list[extracts.ExtractRecord]
) -> None:
    """Fill map_tiles for the area's bbox from the selected extracts
    (select_area_extracts). Tile membership is recorded first so a concurrent
    import's gc cannot evict tiles still being filled. Tiles are re-cut unless
    all were cut from exactly the extracts needed now, at their current
    fetched_at (extracts.tiles_cut_from)."""
    bbox = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])
    needed = tiles.tiles_in_bbox(*bbox)
    total = len(needed)

    conn.execute("DELETE FROM area_tiles WHERE area_id = ?", (area_id,))
    conn.executemany(
        "INSERT INTO area_tiles (area_id, gx, gy) VALUES (?, ?, ?)", [(area_id, gx, gy) for gx, gy in needed]
    )
    conn.commit()

    _set_progress(conn, area_id, phase="tiles", done=0, total=total)
    if extracts.tiles_cut_from(conn, selected, needed):
        metrics.cache("map_tiles", "hit", total)
    else:
        metrics.cache("map_tiles", "miss", total)
        extracts.cut_area(conn, selected, bbox)
    _set_progress(conn, area_id, phase="tiles", done=total, total=total)


def _payload_extent(payloads: Iterable[dict]) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of every node and way-geometry coordinate in
    the payloads, or None when they hold none."""
    lats: list[float] = []
    lngs: list[float] = []
    for payload in payloads:
        for el in payload.get("elements", []):
            points = el.get("geometry") or []
            if el.get("type") == "node":
                points = [el]
            for p in points:
                if p is not None and p.get("lat") is not None and p.get("lon") is not None:
                    lats.append(p["lat"])
                    lngs.append(p["lon"])
    if not lats:
        return None
    return min(lats), min(lngs), max(lats), max(lngs)


def _finish_import(conn: sqlite3.Connection, area_id: int, area: sqlite3.Row) -> None:
    """Elevation prefetch and network build, shared by an extract import and
    import_tiles (tiles uploaded already cut)."""
    needed = [(r["gx"], r["gy"]) for r in conn.execute(
        "SELECT gx, gy FROM area_tiles WHERE area_id = ?", (area_id,)
    ).fetchall()]
    total = len(needed)
    missing = missing_tiles(conn, area_id)
    if missing:
        raise AreaImportError(f"{len(missing)} of {total} map tiles are missing from the cache")

    # DEM tiles are fetched here, outside any transaction, so the build reads
    # the cache alone and never holds the write lock across network calls.
    # Ways run past the area's tile edge, so the range follows the payload
    # coordinates, not the tile boundaries.
    extent = _payload_extent(area_tile_payloads(conn, area_id))
    elevation.ensure_tiles(
        conn,
        bbox=extent,
        on_progress=lambda done, total: _set_progress(conn, area_id, phase="elevation", done=done, total=total),
    )

    _set_progress(conn, area_id, phase="network", done=0, total=1)
    network.build_network(conn, area_id, area_tile_payloads(conn, area_id), elevation.sampler_for(conn))
    _set_progress(conn, area_id, phase="network", done=1, total=1)

    _set_progress(conn, area_id, phase="familiarity", done=0, total=1)
    try:
        familiarity.update(conn, area_id)
    except Exception:
        # Familiarity fills in later from a query; it never fails an import.
        conn.rollback()
        _logger.exception("matching activities to area %s failed", area_id)
    _set_progress(conn, area_id, phase="familiarity", done=1, total=1)


class TilePayload(TypedDict):
    gx: int
    gy: int
    elements: list[dict]


def import_tiles(
    conn: sqlite3.Connection,
    *,
    label: str,
    center_lat: float,
    center_lng: float,
    radius_km: float,
    tile_payloads: list[TilePayload],
    pinned: bool | None = None,
    source: str = "extract:cli",
) -> AreaStatus:
    """Create (or reuse, keyed by label) an area from tiles already cut and
    uploaded by miles-area-import, skipping the tile fetch and running the
    elevation and network tail. For a pbf the API host cannot reach."""
    if not label:
        raise ValueError("import_tiles needs a label")
    gc_areas_and_tiles(conn)
    now = datetime.now(timezone.utc).isoformat()

    area_id = _upsert_area(conn, label, center_lat, center_lng, radius_km, None, pinned, None, now)

    for tile in tile_payloads:
        tilecache.store_tile_elements(conn, tile["gx"], tile["gy"], tile["elements"], source=source)
        conn.execute(
            "INSERT OR IGNORE INTO area_tiles (area_id, gx, gy) VALUES (?, ?, ?)",
            (area_id, tile["gx"], tile["gy"]),
        )
    conn.commit()

    _start_job_or_touch(conn, area_id, now, _run_finish_only_job)
    return get_area_status(conn, area_id)


@contextmanager
def _import_slot(conn: sqlite3.Connection, area_id: int) -> Iterator[None]:
    """One network phase at a time process-wide: each is a burst of requests to
    shared public services. The area reads "queued" until it holds the slot."""
    _set_progress(conn, area_id, phase="queued", done=0, total=1)
    with _import_lock:
        yield


def _run_job(area_id: int, body: Callable[[sqlite3.Connection, int], None]) -> None:
    """Thread target for an import: opens its own connection, runs body, and
    records ready/error in the areas row."""
    conn = db.connect()
    try:
        body(conn, area_id)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE areas SET status = 'ready', status_phase = NULL, status_done = NULL,
                status_total = NULL, status_error = NULL, imported_at = ?, last_used_at = ?
            WHERE area_id = ?
            """,
            (now, now, area_id),
        )
        conn.commit()
    except Exception as e:
        # Uncommitted work of the failed job must not be saved with the error.
        conn.rollback()
        conn.execute(
            "UPDATE areas SET status = 'error', status_error = ? WHERE area_id = ?",
            (describe_error(e), area_id),
        )
        conn.commit()
    finally:
        conn.close()


def _run_import_job(area_id: int, refresh: bool = False, force_extract: bool = False) -> None:
    def body(conn: sqlite3.Connection, aid: int) -> None:
        # The download may wait hours on another import's, so it holds no slot.
        selected = select_area_extracts(conn, aid, refresh, force_extract)
        with _import_slot(conn, aid):
            area = _get_area_row(conn, aid)
            _fetch_tiles_phase(conn, aid, area, selected)
            _finish_import(conn, aid, area)

    _run_job(area_id, body)


def _recut_outdated_tiles(conn: sqlite3.Connection, area_id: int, area: sqlite3.Row) -> None:
    """Re-cut the area's tiles from the extracts already on disk when any was
    cut at an older tilecache.TILE_QUERY_VERSION. Nothing is fetched: the
    area is left as it is unless every one of its tiles came from registered
    extracts whose files are present (tiles uploaded already cut cannot be
    re-cut, and cutting would replace them)."""
    rows = conn.execute(
        """
        SELECT mt.query_version, mt.source FROM area_tiles atl
        JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ?
        """,
        (area_id,),
    ).fetchall()
    if not rows or all(r["query_version"] == tilecache.TILE_QUERY_VERSION for r in rows):
        return
    records: dict[str, extracts.ExtractRecord] = {}
    for row in rows:
        regions = extracts.regions_of_source(row["source"])
        if not regions:
            return
        for region in regions:
            record = extracts.get_extract(conn, region)
            if record is None or not os.path.isfile(record["path"]):
                return
            records[region] = record
    bbox = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])
    _set_progress(conn, area_id, phase="tiles", done=0, total=len(rows))
    metrics.cache("map_tiles", "miss", len(rows))
    extracts.cut_area(conn, list(records.values()), bbox)
    _set_progress(conn, area_id, phase="tiles", done=len(rows), total=len(rows))


def _run_finish_only_job(area_id: int) -> None:
    """Thread target for import_tiles and for rebuilding a stale network: the
    tiles are already stored, so only the elevation + network-build tail runs
    here, after re-cutting tiles an older cut version left behind."""

    def body(conn: sqlite3.Connection, aid: int) -> None:
        with _import_slot(conn, aid):
            area = _get_area_row(conn, aid)
            _recut_outdated_tiles(conn, aid, area)
            _finish_import(conn, aid, area)

    _run_job(area_id, body)


# ---------------------------------------------------------------------------
# Reading an area's cached map data
# ---------------------------------------------------------------------------


def missing_tiles(conn: sqlite3.Connection, area_id: int) -> list[tuple[int, int]]:
    """Tiles the area references that have no map_tiles row."""
    rows = conn.execute(
        """
        SELECT atl.gx, atl.gy FROM area_tiles atl
        LEFT JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ? AND mt.gx IS NULL
        """,
        (area_id,),
    ).fetchall()
    return [(r["gx"], r["gy"]) for r in rows]


def area_tile_payloads(conn: sqlite3.Connection, area_id: int) -> Iterator[dict]:
    """Parsed payload for every tile this area has imported, one dict
    per tile. Raises AreaNotFoundError for an unknown area_id; yields
    nothing for an area whose import hasn't fetched any tiles yet."""
    _get_area_row(conn, area_id)
    rows = conn.execute(
        """
        SELECT mt.data FROM area_tiles atl
        JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ?
        """,
        (area_id,),
    ).fetchall()
    for row in rows:
        yield tilecache.decode_tile_payload(row["data"])
