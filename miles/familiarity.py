"""Familiarity: how often the athlete's own activity tracks have covered each
section of an area, matched from the raw streams already on disk.

Matching is incremental. An activity is matched against an area's sections
(groups, pieces, and stretches alike) once; the result lives in
net_section_visits until a rebuild replaces the sections, which clears it
(sections.delete_area_sections) and so re-matches everything. A stretch counts
as covered by the share of its single-path length the activity ran. A query never matches anything itself: it reads the visits and
reports how many activities are still waiting.
"""

import logging
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import cast

from . import db, network, routes_spatial, sections, streams, terrain
from .tiles import bbox_for_center_radius

_logger = logging.getLogger(__name__)

# A track fix claims the nearest section point within this far.
MATCH_RADIUS_M = 20.0
# Fixes closer together than this add nothing to the match.
FIX_SPACING_M = 12.0
# An edge counts as run when this share of its sample points were claimed.
EDGE_COVERED_SHARE = 0.5
# An activity "covers" a section when it ran this share of it, or this many
# meters of it, whichever is less.
COVER_SHARE = 0.5
COVER_MIN_M = 1000.0
# Activities starting farther than this from the area's edge are not matched.
START_MARGIN_KM = 10.0


@dataclass(frozen=True)
class SectionVisits:
    runs: int
    last_run: str | None


@dataclass(frozen=True)
class FamiliarityStatus:
    matched: int
    pending: int


def _candidates(conn: sqlite3.Connection, area_id: int, include_matched: bool) -> list[sqlite3.Row]:
    area = conn.execute("SELECT center_lat, center_lng, radius_km FROM areas WHERE area_id = ?", [area_id]).fetchone()
    if area is None:
        return []
    south, west, north, east = bbox_for_center_radius(
        area["center_lat"], area["center_lng"], area["radius_km"] + START_MARGIN_KM
    )
    sport_sql, sport_params = terrain.sport_filter()
    matched_sql = "" if include_matched else (
        "AND NOT EXISTS (SELECT 1 FROM net_section_matched m WHERE m.area_id = ? AND m.activity_id = a.activity_id)"
    )
    params = [*sport_params, south, north, west, east, *([] if include_matched else [area_id])]
    rows = conn.execute(
        f"""
        SELECT a.activity_id, a.start_date FROM activities a
        WHERE {sport_sql} AND a.start_lat BETWEEN ? AND ? AND a.start_lng BETWEEN ? AND ? {matched_sql}
        ORDER BY a.start_date DESC
        """,
        params,
    ).fetchall()
    return [r for r in rows if streams.has_cached_streams(r["activity_id"])]


def status(conn: sqlite3.Connection, area_id: int) -> FamiliarityStatus:
    """Activities matched against the area's current sections, and those with
    streams on disk still waiting."""
    matched = conn.execute("SELECT COUNT(*) FROM net_section_matched WHERE area_id = ?", [area_id]).fetchone()[0]
    return FamiliarityStatus(matched, len(_candidates(conn, area_id, include_matched=False)))


def _track_fixes(activity_id: int) -> list[tuple[float, float]]:
    raw = streams.load_raw_streams(activity_id).get("latlng")
    data = raw.get("data") if raw else None
    fixes: list[tuple[float, float]] = []
    if not isinstance(data, list):
        return fixes
    for pair in data:
        if not (isinstance(pair, list) and len(pair) == 2 and None not in pair):
            continue
        lat, lng = float(pair[0]), float(pair[1])
        if not fixes or routes_spatial.equirect_distance_m(fixes[-1][0], fixes[-1][1], lat, lng) >= FIX_SPACING_M:
            fixes.append((lat, lng))
    return fixes


def edge_index(conn: sqlite3.Connection, edge_ids: list[int]) -> tuple[routes_spatial.NearestIndex, dict[int, tuple[float, int]]]:
    """A NearestIndex over the edges' sample points (keyed by edge id, offset
    = point position), and per edge its (length_m, sample point count)."""
    index = routes_spatial.NearestIndex(cell_size_m=40.0)
    edge_info: dict[int, tuple[float, int]] = {}
    for start in range(0, len(edge_ids), 500):
        chunk = edge_ids[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        for row in conn.execute(f"SELECT edge_id, length_m, points_blob FROM net_edges WHERE edge_id IN ({marks})", chunk):
            points = network.unpack_edge_points(bytes(row["points_blob"]))
            edge_info[row["edge_id"]] = (row["length_m"], len(points))
            index.add(row["edge_id"], ((lat, lng, float(k)) for k, (lat, lng, _d, _e) in enumerate(points)))
    return index, edge_info


def update(conn: sqlite3.Connection, area_id: int, max_activities: int | None = None) -> FamiliarityStatus:
    """Match not-yet-matched activities (newest first, at most max_activities)
    against the area's sections and record how far each ran along each."""
    all_sections = sections.load_sections(conn, area_id)
    pending = _candidates(conn, area_id, include_matched=False)
    if max_activities is not None:
        pending = pending[:max_activities]
    built = conn.execute("SELECT built_at FROM net_graphs WHERE area_id = ?", [area_id]).fetchone()
    if not all_sections or not pending or built is None:
        return status(conn, area_id)

    section_of_edge: dict[int, list[int]] = defaultdict(list)
    section_length = {s.section_id: s.single_path_m for s in all_sections}
    for s in all_sections:
        for edge_id in s.edge_ids:
            section_of_edge[edge_id].append(s.section_id)
    index, edge_info = edge_index(conn, sorted(section_of_edge))

    for activity in pending:
        activity_id = activity["activity_id"]
        try:
            fixes = _track_fixes(activity_id)
        except Exception as e:
            _logger.warning("familiarity: unreadable stream for activity %s: %s", activity_id, e)
            fixes = []
        claimed: dict[int, set[int]] = defaultdict(set)
        for lat, lng in fixes:
            hit = index.nearest(lat, lng, MATCH_RADIUS_M)
            if hit is not None:
                claimed[cast(int, hit[0])].add(int(hit[1]))
        covered: dict[int, float] = defaultdict(float)
        for edge_id, points_hit in claimed.items():
            length_m, count = edge_info[edge_id]
            if len(points_hit) / max(count, 1) >= EDGE_COVERED_SHARE:
                for section_id in section_of_edge[edge_id]:
                    covered[section_id] += length_m
        rows = [
            (section_id, activity_id, meters)
            for section_id, meters in covered.items()
            if meters >= min(COVER_SHARE * section_length[section_id], COVER_MIN_M)
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO net_section_visits (section_id, activity_id, covered_m) VALUES (?, ?, ?)", rows
        )
        conn.execute(
            "INSERT OR REPLACE INTO net_section_matched (area_id, activity_id, built_at) VALUES (?, ?, ?)",
            [area_id, activity_id, built["built_at"]],
        )
        conn.commit()
    return status(conn, area_id)


def visits(conn: sqlite3.Connection, area_id: int) -> dict[int, SectionVisits]:
    """Per section: how many activities covered it and the date of the latest."""
    rows = conn.execute(
        """
        SELECT v.section_id, COUNT(*) AS runs, MAX(a.start_date) AS last_run
        FROM net_section_visits v
        JOIN net_sections s ON s.section_id = v.section_id
        JOIN activities a ON a.activity_id = v.activity_id
        WHERE s.area_id = ? GROUP BY v.section_id
        """,
        [area_id],
    ).fetchall()
    return {r["section_id"]: SectionVisits(r["runs"], r["last_run"]) for r in rows}


_running: set[int] = set()
_running_lock = threading.Lock()


def start_background_update(area_id: int) -> bool:
    """Match waiting activities on a worker thread unless one is already
    doing so for the area. Returns whether a thread was started."""
    with _running_lock:
        if area_id in _running:
            return False
        _running.add(area_id)

    def work() -> None:
        conn = db.connect()
        try:
            update(conn, area_id)
        except Exception:
            _logger.exception("familiarity update failed for area %s", area_id)
        finally:
            conn.close()
            with _running_lock:
                _running.discard(area_id)

    threading.Thread(target=work, daemon=True, name=f"familiarity-{area_id}").start()
    return True
