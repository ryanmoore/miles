"""Route database service: upload/derive/query for GPX-backed routes.

Raw GPX bytes (gpx_files) are ground truth; everything else about a route --
resampled points, gain/loss, climbs, grade bands, snapped waypoints -- is
derived from those bytes and rebuilt by derive_all_routes, gated on ROUTE_DERIVE_VERSION exactly like
derive.py gates DERIVE_VERSION. Kept as a separate version stamp and a separate module
because this pipeline is upload-driven, not Strava-sync-driven -- nothing
here should run as part of miles-sync's derive_all pass.

name/source/notes/tags_json are athlete-authored metadata, set once at
upload and changed only via update_route -- re-deriving a route never
touches them, the same way re-deriving an activity never touches its
Strava-sourced fields.
"""

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Literal, NamedTuple, cast
from typing_extensions import NotRequired, TypedDict
from xml.sax.saxutils import escape as xml_escape

from . import db, elevation, gpx_parse, route_locations, routes_spatial
from .fitness import FT_PER_M, MILE_M

# Bumped for DEM draping (elevation_source preference, ele_source,
# elevation_pending) and roughness-based smoothing -- both change what
# _apply_derived computes from the same raw GPX bytes. 5: a KML path whose
# altitudes are partly 0 is read as having no elevation (gpx_parse
# KML_ZERO_ALTITUDE_MAX_SHARE), so it drapes instead.
ROUTE_DERIVE_VERSION = 5

RouteLegKind = Literal["route", "connector"]

# Athlete-set preference (routes.elevation_source) for where a route's
# elevation comes from. "auto" prefers the file and drapes from the terrain
# model only when the file has none; "file" never drapes even when the file
# lacks elevation; "dem" always drapes, ignoring any file elevation.
ElevationSourcePref = Literal["auto", "file", "dem"]
ELEVATION_SOURCE_PREFS: tuple[ElevationSourcePref, ...] = ("auto", "file", "dem")

# What actually populated route_points.ele_m as of the last derive
# (routes.ele_source). Distinct from the preference above: "auto" always
# resolves to one of these two, or to no elevation at all.
ELE_SOURCE_FILE = "file"
ELE_SOURCE_DEM = "dem"


class RouteLegInput(TypedDict):
    """One entry of a compose_route leg list. kind defaults to "route"
    when omitted (the common case); connector legs set kind="connector"
    and skip route_id/from_m/to_m entirely."""
    kind: NotRequired[RouteLegKind]
    route_id: NotRequired[int]
    from_m: NotRequired[float]
    to_m: NotRequired[float]
    distance_m: NotRequired[float]  # connector legs only
    gain_m: NotRequired[float]  # connector legs only, optional
    note: NotRequired[str | None]


class RouteSummary(TypedDict):
    route_id: int
    name: str | None
    gpx_name: str | None
    kind: str
    source: str | None
    tags: list[str]
    has_elevation: bool
    elevation_source: str
    ele_source: str | None
    elevation_pending: bool
    location: str | None  # athlete-set, or looked up from the start point; an imported area's label stands in until then
    distance_m: float | None
    gain_m: float | None
    loss_m: float | None
    ft_per_mi: float | None
    shape: str | None
    start_lat: float | None
    start_lng: float | None


class RouteDetail(RouteSummary):
    file_id: int
    track_index: int
    notes: str | None
    created_at: str
    min_ele_m: float | None
    max_ele_m: float | None
    bbox: list[float] | None  # [south, west, north, east]
    end_lat: float | None
    end_lng: float | None
    climbs: list[gpx_parse.ClimbSegment]
    grade_bands: dict[str, float] | None
    waypoints: list["RouteWaypointOut"]
    legs: list["RouteLegOut"] | None  # only for composed routes


class RouteWaypointOut(TypedDict):
    waypoint_id: int
    name: str | None
    desc: str | None
    lat: float
    lng: float
    dist_m: float
    offset_m: float


class RouteLegOut(TypedDict):
    kind: str
    route_id: int | None
    from_m: float | None
    to_m: float | None
    distance_m: float | None
    gain_m: float | None
    note: str | None


class RouteProfilePoint(TypedDict):
    idx: int
    lat: float
    lng: float
    dist_m: float
    ele_m: float | None


class RouteNearby(TypedDict):
    route_id: int
    name: str | None
    distance_m: float  # closest approach, meters


class ComposeResult(TypedDict):
    legs: list[RouteLegOut]
    distance_m: float
    gain_m: float | None
    loss_m: float | None
    climbs: list[gpx_parse.ClimbSegment]
    grade_bands: dict[str, float] | None
    warnings: list[str]
    route_id: NotRequired[int]  # present only when save=True


class RouteNotFoundError(LookupError):
    pass


def connect() -> sqlite3.Connection:
    """Connection for route endpoints/tools -- separate from mcp_server's/
    api.py's Strava-side _conn(), since route derivation runs on its own
    version stamp (see module docstring)."""
    conn = db.connect()
    ensure_routes_derived(conn)
    return conn


# ---------------------------------------------------------------------------
# Upload and derive
# ---------------------------------------------------------------------------


def upload_gpx(
    conn: sqlite3.Connection,
    data: bytes,
    filename: str,
    *,
    name: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> list[int]:
    """Store data as a gpx_files row (deduped by sha256), create a routes
    row per trk/rte it contains, and derive all of them. Returns the
    route_ids created or refreshed by this call, in document order.
    Accepts KML as well (stored raw, parsed by gpx_parse.parse_kml_root).
    Raises gpx_parse.GpxParseError if the bytes aren't a usable GPX/KML file.
    """
    if len(data) > gpx_parse.MAX_UPLOAD_BYTES:
        raise ValueError(f"GPX upload exceeds {gpx_parse.MAX_UPLOAD_BYTES} bytes")

    doc = gpx_parse.parse_gpx_bytes(data)  # validate before writing anything
    sha256 = hashlib.sha256(data).hexdigest()
    existing = conn.execute("SELECT file_id FROM gpx_files WHERE sha256 = ?", [sha256]).fetchone()
    if existing is not None:
        file_id = int(existing["file_id"])
    else:
        cur = conn.execute(
            """
            INSERT INTO gpx_files (sha256, filename, creator, data, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [sha256, filename, doc["creator"], data, datetime.now(timezone.utc).isoformat()],
        )
        file_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        conn.commit()

    # Uploading a file again is a request for all of its tracks.
    conn.execute("DELETE FROM route_tombstones WHERE file_id = ?", [file_id])
    total_tracks = len(doc["tracks"])
    route_ids: list[int] = []
    for track in doc["tracks"]:
        route_ids.append(
            _ensure_route_shell(
                conn, file_id, track, total_tracks, name=name, source=source, notes=notes, tags=tags
            )
        )
    conn.commit()

    _derive_file(conn, file_id, doc)
    conn.commit()
    return route_ids


def _display_name(user_name: str | None, gpx_name: str | None, track_index: int, total_tracks: int) -> str | None:
    if total_tracks == 1:
        return user_name or gpx_name
    if user_name and gpx_name:
        return f"{user_name} — {gpx_name}"
    if gpx_name:
        return gpx_name
    if user_name:
        return f"{user_name} #{track_index + 1}"
    return None


def _ensure_route_shell(
    conn: sqlite3.Connection,
    file_id: int,
    track: gpx_parse.GpxTrack,
    total_tracks: int,
    *,
    name: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> int:
    row = conn.execute(
        "SELECT route_id FROM routes WHERE file_id = ? AND track_index = ?",
        [file_id, track["track_index"]],
    ).fetchone()
    if row is not None:
        return int(row["route_id"])
    display_name = _display_name(name, track["gpx_name"], track["track_index"], total_tracks)
    cur = conn.execute(
        """
        INSERT INTO routes (
            file_id, track_index, kind, gpx_name, name, source, notes, tags_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            file_id,
            track["track_index"],
            track["kind"],
            track["gpx_name"],
            display_name,
            source,
            notes,
            json.dumps(tags or []),
            datetime.now(timezone.utc).isoformat(),
        ],
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def ensure_routes_derived(conn: sqlite3.Connection) -> None:
    if db.get_meta(conn, "route_derive_version") != str(ROUTE_DERIVE_VERSION):
        derive_all_routes(conn)


def derive_all_routes(conn: sqlite3.Connection) -> dict[str, int]:
    """Re-parse every stored gpx_files blob and rebuild every route derived
    from it. Ground-truth bytes never change, so this is always safe to
    rerun -- the only path guaranteed correct after a parser/threshold
    change (a ROUTE_DERIVE_VERSION bump)."""
    counts = {"files": 0, "routes": 0}
    file_ids = [r["file_id"] for r in conn.execute("SELECT file_id FROM gpx_files").fetchall()]
    for file_id in file_ids:
        counts["routes"] += _derive_file(conn, file_id, None)
        counts["files"] += 1
    db.set_meta(conn, "route_derive_version", str(ROUTE_DERIVE_VERSION))
    conn.commit()
    return counts


def _load_doc(conn: sqlite3.Connection, file_id: int) -> gpx_parse.GpxDocument | None:
    row = conn.execute("SELECT data FROM gpx_files WHERE file_id = ?", [file_id]).fetchone()
    if row is None:
        return None
    return gpx_parse.parse_gpx_bytes(bytes(row["data"]))


def _derive_file(conn: sqlite3.Connection, file_id: int, doc: gpx_parse.GpxDocument | None) -> int:
    if doc is None:
        doc = _load_doc(conn, file_id)
        if doc is None:
            return 0
    total_tracks = len(doc["tracks"])
    deleted = {
        r["track_index"]
        for r in conn.execute("SELECT track_index FROM route_tombstones WHERE file_id = ?", [file_id]).fetchall()
    }
    derived = 0
    for track in doc["tracks"]:
        if track["track_index"] in deleted:
            continue
        route_id = _ensure_route_shell(conn, file_id, track, total_tracks)
        _apply_derived(conn, route_id, track, doc["waypoints"])
        derived += 1
    return derived


def _drape_from_dem(conn: sqlite3.Connection, resampled_latlng: list[tuple[float, float]]) -> list[float] | None:
    """Elevation for every resampled point from the cached terrain model, or
    None if any point falls outside its coverage -- callers treat a partial
    drape as no drape at all, per adr/0002's DEM-draping amendment."""
    values = elevation.sampler_for(conn).sample(resampled_latlng)
    if any(v is None for v in values):
        return None
    return cast(list[float], values)


class _ElevationStats(NamedTuple):
    smoothed: list[float]
    half_window_m: float
    gain_m: float
    loss_m: float
    min_ele_m: float
    max_ele_m: float
    ft_per_mi: float | None


def _elevation_stats(dist_m: list[float], elevations: list[float], total_distance: float) -> _ElevationStats:
    """The profile smoothed with a window chosen from its own roughness, and
    its gain, loss, extremes, and gain per mile."""
    smoothed, half_window_m = gpx_parse.roughness_smoothed(dist_m, elevations)
    gain_m, loss_m = gpx_parse.gain_loss_m(smoothed)
    ft_per_mi = (gain_m * FT_PER_M) / (total_distance / MILE_M) if total_distance > 0 else None
    return _ElevationStats(smoothed, half_window_m, gain_m, loss_m, min(elevations), max(elevations), ft_per_mi)


def _apply_derived(
    conn: sqlite3.Connection,
    route_id: int,
    track: gpx_parse.GpxTrack,
    file_waypoints: list[gpx_parse.GpxWaypoint],
) -> None:
    """Rebuild everything about a route from its raw GPX track. A DEM drape
    (elevation_source 'auto' without file elevation, or 'dem') reads only the
    tile cache, so this never blocks startup or a request on a fetch;
    redrape_route fetches the tiles first."""
    points = track["points"]
    dist_m = gpx_parse.cumulative_distances_m(points)
    total_distance = dist_m[-1]
    has_file_ele = gpx_parse.has_full_elevation(points)

    pref_row = conn.execute("SELECT elevation_source FROM routes WHERE route_id = ?", [route_id]).fetchone()
    preference = cast(ElevationSourcePref, (pref_row["elevation_source"] if pref_row else None) or "auto")

    use_file = has_file_ele and preference != "dem"
    want_dem = preference == "dem" or (preference == "auto" and not has_file_ele)

    half_window_m = gpx_parse.ELEVATION_SMOOTHING_HALF_WINDOW_M
    gain_m = loss_m = min_ele_m = max_ele_m = ft_per_mi = None
    ele_source: str | None = None

    if use_file:
        stats = _elevation_stats(dist_m, cast(list[float], [p[2] for p in points]), total_distance)
        half_window_m = stats.half_window_m
        gain_m, loss_m, min_ele_m, max_ele_m, ft_per_mi = stats[2:]
        ele_source = ELE_SOURCE_FILE

    south, west, north, east = gpx_parse.bbox(points)
    start_lat, start_lng = points[0][0], points[0][1]
    end_lat, end_lng = points[-1][0], points[-1][1]

    # Positions always resample the same way regardless of elevation source;
    # half_window_m only matters when use_file left resample_points' own
    # elevation column in play below.
    resampled = gpx_parse.resample_points(points, dist_m, half_window_m=half_window_m)
    resampled_latlng = [(p[0], p[1]) for p in resampled]
    resampled_dist = [p[2] for p in resampled]
    resampled_ele: list[float | None] = [p[3] for p in resampled] if use_file else [None] * len(resampled)
    shape = routes_spatial.classify_shape(resampled_latlng, resampled_dist)

    elevation_pending = False
    if want_dem:
        dem_values = _drape_from_dem(conn, resampled_latlng)
        if dem_values is None:
            elevation_pending = True
            if preference == "dem":
                # An explicit DEM preference ignores file elevation entirely,
                # so a failed drape leaves the route without elevation --
                # never a silent fall back to a source the athlete opted out of.
                ele_source = None
                gain_m = loss_m = min_ele_m = max_ele_m = ft_per_mi = None
                resampled_ele = [None] * len(resampled_dist)
            # preference == "auto": already without elevation (use_file was False).
        else:
            stats = _elevation_stats(resampled_dist, dem_values, total_distance)
            gain_m, loss_m, min_ele_m, max_ele_m, ft_per_mi = stats[2:]
            ele_source = ELE_SOURCE_DEM
            resampled_ele = cast(list[float | None], stats.smoothed)

    has_ele = ele_source is not None

    conn.execute("DELETE FROM route_points WHERE route_id = ?", [route_id])
    conn.execute("DELETE FROM route_climbs WHERE route_id = ?", [route_id])
    conn.execute("DELETE FROM route_grade_bands WHERE route_id = ?", [route_id])
    conn.execute("DELETE FROM route_waypoints WHERE route_id = ?", [route_id])

    point_rows = zip(resampled_latlng, resampled_dist, resampled_ele)
    conn.executemany(
        "INSERT INTO route_points (route_id, idx, lat, lng, dist_m, ele_m) VALUES (?, ?, ?, ?, ?, ?)",
        [(route_id, idx, lat, lng, d, ele) for idx, ((lat, lng), d, ele) in enumerate(point_rows)],
    )

    if has_ele:
        final_ele = cast(list[float], resampled_ele)
        climbs = gpx_parse.segment_climbs(resampled_dist, final_ele)
        conn.executemany(
            "INSERT INTO route_climbs (route_id, idx, kind, start_m, end_m, gain_m, avg_grade) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(route_id, i, c["kind"], c["start_m"], c["end_m"], c["gain_m"], c["avg_grade"]) for i, c in enumerate(climbs)],
        )
        bands = gpx_parse.grade_band_histogram(resampled_dist, final_ele)
        conn.executemany(
            "INSERT INTO route_grade_bands (route_id, band, share) VALUES (?, ?, ?)",
            [(route_id, band, share) for band, share in bands.items()],
        )

    for wp in file_waypoints:
        nearest_idx = min(
            range(len(points)),
            key=lambda i: routes_spatial.equirect_distance_m(wp["lat"], wp["lng"], points[i][0], points[i][1]),
        )
        offset = routes_spatial.equirect_distance_m(
            wp["lat"], wp["lng"], points[nearest_idx][0], points[nearest_idx][1]
        )
        conn.execute(
            "INSERT INTO route_waypoints (route_id, name, desc, lat, lng, dist_m, offset_m) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [route_id, wp["name"], wp["desc"], wp["lat"], wp["lng"], dist_m[nearest_idx], offset],
        )

    conn.execute(
        """
        UPDATE routes SET
            gpx_name = ?, derive_version = ?, has_elevation = ?, ele_source = ?, elevation_pending = ?,
            distance_m = ?, gain_m = ?, loss_m = ?, min_ele_m = ?, max_ele_m = ?, ft_per_mi = ?,
            bbox_south = ?, bbox_west = ?, bbox_north = ?, bbox_east = ?,
            start_lat = ?, start_lng = ?, end_lat = ?, end_lng = ?, shape = ?
        WHERE route_id = ?
        """,
        [
            track["gpx_name"], ROUTE_DERIVE_VERSION, int(has_ele), ele_source, int(elevation_pending),
            total_distance, gain_m, loss_m, min_ele_m, max_ele_m, ft_per_mi,
            south, west, north, east,
            start_lat, start_lng, end_lat, end_lng, shape,
            route_id,
        ],
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _row_to_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> RouteSummary:
    return {
        "route_id": row["route_id"],
        "name": row["name"],
        "gpx_name": row["gpx_name"],
        "kind": row["kind"],
        "source": row["source"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "has_elevation": bool(row["has_elevation"]),
        "elevation_source": row["elevation_source"],
        "ele_source": row["ele_source"],
        "elevation_pending": bool(row["elevation_pending"]),
        "location": route_locations.stored_or_area_label(conn, row),
        "distance_m": row["distance_m"],
        "gain_m": row["gain_m"],
        "loss_m": row["loss_m"],
        "ft_per_mi": row["ft_per_mi"],
        "shape": row["shape"],
        "start_lat": row["start_lat"],
        "start_lng": row["start_lng"],
    }


def _route_points_latlng(conn: sqlite3.Connection, route_id: int) -> list[tuple[float, float]]:
    rows = conn.execute(
        "SELECT lat, lng FROM route_points WHERE route_id = ? ORDER BY idx", [route_id]
    ).fetchall()
    return [(r["lat"], r["lng"]) for r in rows]


def list_routes(
    conn: sqlite3.Connection,
    *,
    min_distance_m: float | None = None,
    max_distance_m: float | None = None,
    min_ft_per_mi: float | None = None,
    max_ft_per_mi: float | None = None,
    tag: str | None = None,
    near_lat: float | None = None,
    near_lng: float | None = None,
    near_radius_m: float = 5000.0,
) -> list[RouteSummary]:
    where: list[str] = []
    params: list[object] = []
    if min_distance_m is not None:
        where.append("distance_m >= ?")
        params.append(min_distance_m)
    if max_distance_m is not None:
        where.append("distance_m <= ?")
        params.append(max_distance_m)
    if min_ft_per_mi is not None:
        where.append("ft_per_mi >= ?")
        params.append(min_ft_per_mi)
    if max_ft_per_mi is not None:
        where.append("ft_per_mi <= ?")
        params.append(max_ft_per_mi)
    sql = "SELECT * FROM routes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    summaries = [_row_to_summary(conn, r) for r in conn.execute(sql, params).fetchall()]

    if tag:
        summaries = [s for s in summaries if tag in s["tags"]]

    if near_lat is not None and near_lng is not None:
        kept = []
        for s in summaries:
            pts = _route_points_latlng(conn, s["route_id"])
            if pts and routes_spatial.routes_near_point(near_lat, near_lng, near_radius_m, pts) is not None:
                kept.append(s)
        summaries = kept

    return summaries


def _get_route_row(conn: sqlite3.Connection, route_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM routes WHERE route_id = ?", [route_id]).fetchone()
    if row is None:
        raise RouteNotFoundError(f"No route {route_id}")
    return row


def get_route(conn: sqlite3.Connection, route_id: int) -> RouteDetail:
    row = _get_route_row(conn, route_id)
    climbs: list[gpx_parse.ClimbSegment] = [
        {
            "kind": r["kind"],
            "start_m": r["start_m"],
            "end_m": r["end_m"],
            "gain_m": r["gain_m"],
            "avg_grade": r["avg_grade"],
        }
        for r in conn.execute(
            "SELECT kind, start_m, end_m, gain_m, avg_grade FROM route_climbs WHERE route_id = ? ORDER BY idx",
            [route_id],
        ).fetchall()
    ]
    band_rows = conn.execute(
        "SELECT band, share FROM route_grade_bands WHERE route_id = ?", [route_id]
    ).fetchall()
    grade_bands = {r["band"]: r["share"] for r in band_rows} if band_rows else None
    waypoints: list[RouteWaypointOut] = [
        {
            "waypoint_id": r["waypoint_id"],
            "name": r["name"],
            "desc": r["desc"],
            "lat": r["lat"],
            "lng": r["lng"],
            "dist_m": r["dist_m"],
            "offset_m": r["offset_m"],
        }
        for r in conn.execute(
            "SELECT waypoint_id, name, desc, lat, lng, dist_m, offset_m FROM route_waypoints WHERE route_id = ? ORDER BY dist_m",
            [route_id],
        ).fetchall()
    ]
    leg_rows = conn.execute(
        "SELECT kind, leg_route_id, from_m, to_m, distance_m, gain_m, note FROM route_legs WHERE route_id = ? ORDER BY idx",
        [route_id],
    ).fetchall()
    legs: list[RouteLegOut] | None = (
        [
            {
                "kind": r["kind"],
                "route_id": r["leg_route_id"],
                "from_m": r["from_m"],
                "to_m": r["to_m"],
                "distance_m": r["distance_m"],
                "gain_m": r["gain_m"],
                "note": r["note"],
            }
            for r in leg_rows
        ]
        if leg_rows
        else None
    )
    bbox = None
    if row["bbox_south"] is not None:
        bbox = [row["bbox_south"], row["bbox_west"], row["bbox_north"], row["bbox_east"]]

    summary = _row_to_summary(conn, row)
    return {
        **summary,
        "file_id": row["file_id"],
        "track_index": row["track_index"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "min_ele_m": row["min_ele_m"],
        "max_ele_m": row["max_ele_m"],
        "bbox": bbox,
        "end_lat": row["end_lat"],
        "end_lng": row["end_lng"],
        "climbs": climbs,
        "grade_bands": grade_bands,
        "waypoints": waypoints,
        "legs": legs,
    }


def get_route_gpx(conn: sqlite3.Connection, route_id: int) -> tuple[str, bytes]:
    """(filename, bytes) of the file backing this route: the raw upload, or
    for a KML upload a GPX conversion of it."""
    row = conn.execute(
        """
        SELECT f.filename, f.data FROM routes r JOIN gpx_files f ON f.file_id = r.file_id
        WHERE r.route_id = ?
        """,
        [route_id],
    ).fetchone()
    if row is None:
        raise RouteNotFoundError(f"No route {route_id}")
    filename, data = str(row["filename"]), bytes(row["data"])
    if gpx_parse.is_kml(data):
        # Stored as uploaded (raw bytes are ground truth); served as GPX.
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        return f"{stem}.gpx", gpx_parse.document_to_gpx(gpx_parse.parse_gpx_bytes(data))
    return filename, data


def get_route_profile(conn: sqlite3.Connection, route_id: int) -> list[RouteProfilePoint]:
    _get_route_row(conn, route_id)
    rows = conn.execute(
        "SELECT idx, lat, lng, dist_m, ele_m FROM route_points WHERE route_id = ? ORDER BY idx",
        [route_id],
    ).fetchall()
    return [
        {"idx": r["idx"], "lat": r["lat"], "lng": r["lng"], "dist_m": r["dist_m"], "ele_m": r["ele_m"]}
        for r in rows
    ]


def update_route(
    conn: sqlite3.Connection,
    route_id: int,
    *,
    name: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    elevation_source: str | None = None,
    location: str | None = None,
) -> RouteDetail:
    """Partial update: only fields passed as non-None are changed. There is
    no way to blank out name/notes back to NULL through this call -- pass a
    new value instead; tags can be cleared with an empty list.

    A non-empty location is the athlete's and is never replaced by a lookup;
    an empty string clears it, so the next view looks the place up again.

    Changing elevation_source (auto/file/dem) re-derives the route's
    elevation at once from cached tiles only; a route that needs an uncached
    tile comes back pending (see redrape_route).
    """
    row = _get_route_row(conn, route_id)
    sets: list[str] = []
    params: list[object] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    if tags is not None:
        sets.append("tags_json = ?")
        params.append(json.dumps(tags))
    if location is not None:
        label = location.strip()
        sets.append("location = ?")
        params.append(label or None)
        sets.append("location_source = ?")
        params.append(route_locations.SOURCE_ATHLETE if label else None)
    if elevation_source is not None:
        if elevation_source not in ELEVATION_SOURCE_PREFS:
            raise ValueError(f"elevation_source must be one of {ELEVATION_SOURCE_PREFS}, got {elevation_source!r}")
        sets.append("elevation_source = ?")
        params.append(elevation_source)
    if sets:
        params.append(route_id)
        conn.execute(f"UPDATE routes SET {', '.join(sets)} WHERE route_id = ?", params)
        conn.commit()
    if elevation_source is not None and elevation_source != row["elevation_source"]:
        _redrape_route(conn, route_id)
    return get_route(conn, route_id)


def _redrape_route(conn: sqlite3.Connection, route_id: int, *, fetch_tiles: bool = False) -> None:
    row = _get_route_row(conn, route_id)
    doc = _load_doc(conn, row["file_id"])
    if doc is None:
        raise RouteNotFoundError(f"No route {route_id}")
    track = next((t for t in doc["tracks"] if t["track_index"] == row["track_index"]), None)
    if track is None:
        raise RouteNotFoundError(f"No route {route_id}")
    if fetch_tiles:
        elevation.ensure_tiles(conn, points=[(p[0], p[1]) for p in track["points"]])
    _apply_derived(conn, route_id, track, doc["waypoints"])
    conn.commit()


def redrape_route(conn: sqlite3.Connection, route_id: int) -> RouteDetail:
    """Re-derive one route's elevation, fetching any missing tiles; every
    other derive path reads only the tile cache. Retries a pending drape."""
    _redrape_route(conn, route_id, fetch_tiles=True)
    return get_route(conn, route_id)


def delete_route(conn: sqlite3.Connection, route_id: int) -> None:
    """Remove a route and what was derived for it. Its GPX file goes too when
    no other track of that file still has a route; otherwise the track is
    tombstoned so a re-derivation does not bring it back."""
    row = _get_route_row(conn, route_id)
    file_id, track_index = row["file_id"], row["track_index"]
    for table in ("route_points", "route_waypoints", "route_climbs", "route_grade_bands", "route_legs"):
        conn.execute(f"DELETE FROM {table} WHERE route_id = ?", [route_id])
    conn.execute("UPDATE route_legs SET leg_route_id = NULL WHERE leg_route_id = ?", [route_id])
    conn.execute("DELETE FROM routes WHERE route_id = ?", [route_id])
    siblings = conn.execute("SELECT COUNT(*) FROM routes WHERE file_id = ?", [file_id]).fetchone()[0]
    if siblings:
        conn.execute(
            "INSERT OR IGNORE INTO route_tombstones (file_id, track_index) VALUES (?, ?)", [file_id, track_index]
        )
    else:
        conn.execute("DELETE FROM gpx_files WHERE file_id = ?", [file_id])
        conn.execute("DELETE FROM route_tombstones WHERE file_id = ?", [file_id])
    conn.commit()


# ---------------------------------------------------------------------------
# Spatial queries
# ---------------------------------------------------------------------------


class FindJunctionsResult(TypedDict):
    route_a: int
    route_b: int
    junctions: list[routes_spatial.Junction]
    overlaps: list[routes_spatial.Overlap]


def find_route_junctions(
    conn: sqlite3.Connection, route_a: int, route_b: int, tolerance_m: float = 30.0
) -> FindJunctionsResult:
    a_rows = conn.execute(
        "SELECT lat, lng, dist_m FROM route_points WHERE route_id = ? ORDER BY idx", [route_a]
    ).fetchall()
    b_rows = conn.execute(
        "SELECT lat, lng, dist_m FROM route_points WHERE route_id = ? ORDER BY idx", [route_b]
    ).fetchall()
    if not a_rows or not b_rows:
        raise RouteNotFoundError("Both routes must have derived points")
    result = routes_spatial.find_junctions(
        [(r["lat"], r["lng"]) for r in a_rows],
        [r["dist_m"] for r in a_rows],
        [(r["lat"], r["lng"]) for r in b_rows],
        [r["dist_m"] for r in b_rows],
        tolerance_m=tolerance_m,
    )
    return {"route_a": route_a, "route_b": route_b, **result}


def routes_near(
    conn: sqlite3.Connection, lat: float, lng: float, radius_m: float
) -> list[RouteNearby]:
    rows = conn.execute("SELECT route_id, name FROM routes WHERE distance_m IS NOT NULL").fetchall()
    out: list[RouteNearby] = []
    for r in rows:
        pts = _route_points_latlng(conn, r["route_id"])
        if not pts:
            continue
        d = routes_spatial.routes_near_point(lat, lng, radius_m, pts)
        if d is not None:
            out.append({"route_id": r["route_id"], "name": r["name"], "distance_m": d})
    out.sort(key=lambda x: x["distance_m"])
    return out


def routes_near_route(conn: sqlite3.Connection, route_id: int, radius_m: float) -> list[RouteNearby]:
    anchor_points = _route_points_latlng(conn, route_id)
    if not anchor_points:
        raise RouteNotFoundError(f"No route {route_id}")
    anchor_index = routes_spatial.point_index(anchor_points, radius_m)
    rows = conn.execute(
        "SELECT route_id, name FROM routes WHERE distance_m IS NOT NULL AND route_id != ?", [route_id]
    ).fetchall()
    out: list[RouteNearby] = []
    for r in rows:
        pts = _route_points_latlng(conn, r["route_id"])
        hits = (anchor_index.nearest(lat, lng, radius_m) for lat, lng in pts)
        best = min((hit[2] for hit in hits if hit is not None), default=None)
        if best is not None:
            out.append({"route_id": r["route_id"], "name": r["name"], "distance_m": best})
    out.sort(key=lambda x: x["distance_m"])
    return out


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def _route_point_series(conn: sqlite3.Connection, route_id: int) -> tuple[list[float], list[float], list[float], list[float | None]]:
    rows = conn.execute(
        "SELECT lat, lng, dist_m, ele_m FROM route_points WHERE route_id = ? ORDER BY idx", [route_id]
    ).fetchall()
    return (
        [r["dist_m"] for r in rows],
        [r["lat"] for r in rows],
        [r["lng"] for r in rows],
        [r["ele_m"] for r in rows],
    )


def compose_route(
    conn: sqlite3.Connection,
    legs: list[RouteLegInput],
    *,
    tolerance_m: float = 30.0,
    save: bool = False,
    name: str | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> ComposeResult:
    """Stitch an ordered list of route legs (partial spans of existing
    routes, direction given by from_m vs to_m, or connector legs for
    stretches with no GPX) into one combined run. Returns combined
    distance/gain/loss, a stitched climb/grade profile, and warnings where
    consecutive legs don't meet within tolerance_m. Elevation gaps at
    connectors (and at legs from a no-elevation route) break the profile
    into separate chunks for climb/grade purposes, so a connector never
    fabricates a climb across a stretch we have no data for.
    """
    total_distance = 0.0
    total_gain = 0.0
    total_loss = 0.0
    gain_known = False  # any leg (route or connector) actually contributed a gain figure
    loss_known = False  # only route legs with elevation ever contribute loss
    warnings: list[str] = []
    leg_summaries: list[RouteLegOut] = []

    # For the saved GPX / climb-and-grade profile: contiguous elevation
    # chunks, broken at connectors and at any leg lacking elevation.
    chunks: list[tuple[list[float], list[float]]] = []
    cur_chunk_dist: list[float] = []
    cur_chunk_ele: list[float] = []
    # Full stitched positions, for the optionally-saved GPX (ele holes
    # simply carry None there).
    profile_lat: list[float] = []
    profile_lng: list[float] = []
    profile_ele: list[float | None] = []

    def flush_chunk() -> None:
        nonlocal cur_chunk_dist, cur_chunk_ele
        if len(cur_chunk_dist) >= 2:
            chunks.append((cur_chunk_dist, cur_chunk_ele))
        cur_chunk_dist = []
        cur_chunk_ele = []

    prev_end: tuple[float, float] | None = None
    for i, leg in enumerate(legs):
        kind = leg.get("kind", "route")
        if kind == "connector":
            flush_chunk()
            leg_distance_m = leg.get("distance_m")
            if leg_distance_m is None:
                raise ValueError(f"leg {i}: a connector leg needs distance_m")
            dist = float(leg_distance_m)
            gain = leg.get("gain_m")
            total_distance += dist
            if gain is not None:
                total_gain += gain
                gain_known = True
            leg_summaries.append(
                {"kind": "connector", "route_id": None, "from_m": None, "to_m": None,
                 "distance_m": dist, "gain_m": gain, "note": leg.get("note")}
            )
            prev_end = None
            continue

        leg_route_id = leg.get("route_id")
        leg_from_m = leg.get("from_m")
        leg_to_m = leg.get("to_m")
        if leg_route_id is None or leg_from_m is None or leg_to_m is None:
            raise ValueError(f"leg {i}: a route leg needs route_id, from_m, and to_m")
        route_id = int(leg_route_id)
        from_m = float(leg_from_m)
        to_m = float(leg_to_m)
        route = _get_route_row(conn, route_id)
        dist_list, lat_list, lng_list, ele_list = _route_point_series(conn, route_id)
        if not dist_list:
            raise RouteNotFoundError(f"Route {route_id} has no derived points")

        seg_dist, seg_lat_f = routes_spatial.slice_by_distance(dist_list, cast(list[float | None], lat_list), from_m, to_m)
        _, seg_lng_f = routes_spatial.slice_by_distance(dist_list, cast(list[float | None], lng_list), from_m, to_m)
        _, seg_ele = routes_spatial.slice_by_distance(dist_list, ele_list, from_m, to_m)
        if not seg_dist:
            warnings.append(f"leg {i}: from_m/to_m out of range for route {route_id}, skipped")
            continue
        seg_lat = cast(list[float], seg_lat_f)
        seg_lng = cast(list[float], seg_lng_f)

        if prev_end is not None:
            gap = routes_spatial.equirect_distance_m(prev_end[0], prev_end[1], seg_lat[0], seg_lng[0])
            if gap > tolerance_m:
                warnings.append(f"leg {i}: {gap:.0f}m gap from the previous leg's end (tolerance {tolerance_m:.0f}m)")
        prev_end = (seg_lat[-1], seg_lng[-1])

        base = total_distance
        for d, lat, lng, ele in zip(seg_dist, seg_lat, seg_lng, seg_ele):
            profile_lat.append(lat)
            profile_lng.append(lng)
            profile_ele.append(ele)
            if ele is not None:
                cur_chunk_dist.append(base + d)
                cur_chunk_ele.append(ele)
            else:
                flush_chunk()

        leg_distance = seg_dist[-1]
        leg_gain: float | None = None
        leg_loss: float | None = None
        if route["has_elevation"] and all(e is not None for e in seg_ele):
            leg_gain, leg_loss = gpx_parse.gain_loss_m(cast(list[float], seg_ele))
            total_gain += leg_gain
            total_loss += leg_loss
            gain_known = True
            loss_known = True
        else:
            warnings.append(f"leg {i}: route {route_id} has no elevation data; gain/loss totals are partial")

        total_distance += leg_distance
        leg_summaries.append(
            {"kind": "route", "route_id": route_id, "from_m": from_m, "to_m": to_m,
             "distance_m": leg_distance, "gain_m": leg_gain, "note": None}
        )
    flush_chunk()

    all_climbs: list[gpx_parse.ClimbSegment] = []
    band_totals = {label: 0.0 for _, _, label in gpx_parse.GRADE_BANDS}
    band_total_dist = 0.0
    for chunk_dist, chunk_ele in chunks:
        all_climbs.extend(gpx_parse.segment_climbs(chunk_dist, chunk_ele))
        chunk_bands = gpx_parse.grade_band_histogram(chunk_dist, chunk_ele)
        chunk_len = chunk_dist[-1] - chunk_dist[0]
        for label, share in chunk_bands.items():
            band_totals[label] += share * chunk_len
        band_total_dist += chunk_len
    grade_bands = {label: v / band_total_dist for label, v in band_totals.items()} if band_total_dist > 0 else None

    result: ComposeResult = {
        "legs": leg_summaries,
        "distance_m": total_distance,
        "gain_m": total_gain if gain_known else None,
        "loss_m": total_loss if loss_known else None,
        "climbs": all_climbs,
        "grade_bands": grade_bands,
        "warnings": warnings,
    }

    if save:
        route_id = _save_composed_route(conn, profile_lat, profile_lng, profile_ele, legs, name, notes, tags)
        result["route_id"] = route_id
    return result


def build_gpx(name: str, points: list[tuple[float, float, float | None]]) -> bytes:
    lines = [
        '<?xml version="1.0"?>',
        f'<gpx version="1.1" creator="miles" xmlns="{gpx_parse.GPX_NAMESPACE}">',
        "  <trk>",
        f"    <name>{xml_escape(name)}</name>",
        "    <trkseg>",
    ]
    for lat, lng, ele in points:
        if ele is not None:
            lines.append(f'      <trkpt lat="{lat:.7f}" lon="{lng:.7f}"><ele>{ele:.2f}</ele></trkpt>')
        else:
            lines.append(f'      <trkpt lat="{lat:.7f}" lon="{lng:.7f}"/>')
    lines += ["    </trkseg>", "  </trk>", "</gpx>"]
    return "\n".join(lines).encode("utf-8")


def _save_composed_route(
    conn: sqlite3.Connection,
    profile_lat: list[float],
    profile_lng: list[float],
    profile_ele: list[float | None],
    legs: list[RouteLegInput],
    name: str | None,
    notes: str | None,
    tags: list[str] | None,
) -> int:
    route_name = name or "Composed route"
    points = list(zip(profile_lat, profile_lng, profile_ele))
    gpx_bytes = build_gpx(route_name, points)
    # A composed GPX from live legs is effectively unique every time (even
    # identical legs re-run now carry a fresh timestamp in the filename);
    # sha256 dedup still protects against re-saving the exact same bytes.
    filename = f"composed-{int(time.time())}.gpx"
    route_ids = upload_gpx(conn, gpx_bytes, filename, name=route_name, source="composed", notes=notes, tags=tags)
    route_id = route_ids[0]

    leg_rows = []
    for i, leg in enumerate(legs):
        kind = leg.get("kind", "route")
        leg_rows.append(
            (
                route_id,
                i,
                kind,
                leg.get("route_id") if kind == "route" else None,
                leg.get("from_m") if kind == "route" else None,
                leg.get("to_m") if kind == "route" else None,
                leg.get("distance_m"),
                leg.get("gain_m"),
                leg.get("note"),
            )
        )
    conn.executemany(
        """
        INSERT INTO route_legs (route_id, idx, kind, leg_route_id, from_m, to_m, distance_m, gain_m, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        leg_rows,
    )
    conn.commit()
    return route_id


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


class CompareSide(TypedDict):
    route_id: int
    name: str | None
    distance_m: float | None
    has_elevation: bool
    gain_m: float | None
    loss_m: float | None
    ft_per_mi: float | None
    grade_bands: dict[str, float] | None
    climbs: list[gpx_parse.ClimbSegment]
    descents_by_fraction: list[dict[str, float]]


class CompareResult(TypedDict):
    route: CompareSide
    course: CompareSide
    grade_histogram_distance: float | None
    warnings: list[str]


def _compare_side(conn: sqlite3.Connection, route_id: int) -> CompareSide:
    detail = get_route(conn, route_id)
    distance = detail["distance_m"]
    descents = []
    if distance:
        for c in detail["climbs"]:
            if c["kind"] == "descent":
                descents.append(
                    {
                        "start_fraction": c["start_m"] / distance,
                        "end_fraction": c["end_m"] / distance,
                        "gain_m": c["gain_m"],
                    }
                )
    return {
        "route_id": route_id,
        "name": detail["name"] or detail["gpx_name"],
        "distance_m": distance,
        "has_elevation": detail["has_elevation"],
        "gain_m": detail["gain_m"],
        "loss_m": detail["loss_m"],
        "ft_per_mi": detail["ft_per_mi"],
        "grade_bands": detail["grade_bands"],
        "climbs": detail["climbs"],
        "descents_by_fraction": descents,
    }


def compare_route_to_course(conn: sqlite3.Connection, route_id: int, course_route_id: int) -> CompareResult:
    """Transparent side-by-side metrics for two routes -- ft/mi, grade
    histograms (plus their L1 distance, the only aggregate this returns),
    climb lists, and each route's sustained descents as a fraction of
    total distance. No blended single score."""
    route_side = _compare_side(conn, route_id)
    course_side = _compare_side(conn, course_route_id)
    warnings: list[str] = []
    histogram_distance = None
    route_bands = route_side["grade_bands"]
    course_bands = course_side["grade_bands"]
    if route_bands is not None and course_bands is not None:
        histogram_distance = sum(abs(route_bands[label] - course_bands[label]) for label in route_bands)
    else:
        if route_side["grade_bands"] is None:
            warnings.append(f"route {route_id} has no elevation data; grade comparison unavailable")
        if course_side["grade_bands"] is None:
            warnings.append(f"route {course_route_id} has no elevation data; grade comparison unavailable")
    return {
        "route": route_side,
        "course": course_side,
        "grade_histogram_distance": histogram_distance,
        "warnings": warnings,
    }
