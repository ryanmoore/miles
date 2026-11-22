"""Where a stretch of a recorded run was: coordinates, elevation profile from
both altitude sources, and the named ways under it from an imported area."""

import math
import sqlite3

import numpy as np
import pandas as pd

from . import elevation, familiarity, streams
from .terrain import TerrainDataError
from .tiles import bbox_for_center_radius

# Spacing of the resampled profile. Altitude gets only a short moving average
# over it, so a hill much shorter than the 100 m gpx_parse smoothing survives.
STEP_M = 5.0
SMOOTHING_SAMPLES = 5
MAX_POINTS = 40
# A way entry shorter than this between two entries of the same way is a
# junction or a parallel way the track passed, not a change of way.
BLIP_M = 15.0


def _covering_area(conn: sqlite3.Connection, lat: float, lng: float) -> sqlite3.Row | None:
    for area in conn.execute(
        "SELECT a.area_id, a.label, a.center_lat, a.center_lng, a.radius_km FROM areas a "
        "JOIN net_graphs g ON g.area_id = a.area_id ORDER BY a.area_id"
    ):
        south, west, north, east = bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])
        if south <= lat <= north and west <= lng <= east:
            return area
    return None


def _ways(conn: sqlite3.Connection, area_id: int, dist: np.ndarray, lat: np.ndarray, lng: np.ndarray) -> list[dict[str, object]]:
    """Consecutive grid points on the same way as one entry. A point with no
    edge within the match radius is skipped; an entry runs until the next
    point (matched or not) after its last one."""
    edges = {
        r["edge_id"]: r
        for r in conn.execute(
            "SELECT edge_id, way_id, way_name, highway, surface_class FROM net_edges WHERE area_id = ?", [area_id]
        )
    }
    index, _ = familiarity.edge_index(conn, list(edges))
    ways: list[dict[str, object]] = []
    for i in range(len(dist)):
        hit = index.nearest(float(lat[i]), float(lng[i]), familiarity.MATCH_RADIUS_M)
        if hit is None:
            continue
        edge = edges[hit[0]]
        end_m = float(dist[min(i + 1, len(dist) - 1)])
        if ways and ways[-1]["way_id"] == edge["way_id"]:
            ways[-1]["end_m"] = end_m
        else:
            ways.append({
                "start_m": float(dist[i]), "end_m": end_m, "way_id": edge["way_id"], "name": edge["way_name"],
                "highway": edge["highway"], "surface": edge["surface_class"],
            })
    return _absorb_blips(ways)


def _absorb_blips(ways: list[dict[str, object]]) -> list[dict[str, object]]:
    """Join the entries either side of a BLIP_M blip into one; round the distances."""
    merged: list[dict[str, object]] = []
    for i, way in enumerate(ways):
        short = float(way["end_m"]) - float(way["start_m"]) < BLIP_M  # type: ignore[arg-type]
        if 0 < i < len(ways) - 1 and short and ways[i - 1]["way_id"] == ways[i + 1]["way_id"]:
            continue
        if merged and merged[-1]["way_id"] == way["way_id"]:
            merged[-1]["end_m"] = way["end_m"]
        else:
            merged.append(dict(way))
    return [{**w, "start_m": round(float(w["start_m"]), 1), "end_m": round(float(w["end_m"]), 1)} for w in merged]  # type: ignore[arg-type]


def _profile(ele: list[float | None], dist: np.ndarray, source: str) -> dict[str, object] | None:
    if not ele or any(e is None for e in ele):
        return None
    smooth = np.asarray(pd.Series(ele, dtype=float).rolling(SMOOTHING_SAMPLES, center=True, min_periods=1).mean())
    rises = np.diff(smooth)
    length = float(dist[-1] - dist[0])
    return {
        "source": source,
        "start_ele_m": round(float(smooth[0]), 1),
        "end_ele_m": round(float(smooth[-1]), 1),
        "gain_m": round(float(rises[rises > 0].sum()), 1),
        "loss_m": round(float(abs(rises[rises < 0].sum())), 1),
        "avg_grade_pct": round(float(smooth[-1] - smooth[0]) / length * 100.0, 1) if length > 0 else None,
    }


def activity_stretch(
    conn: sqlite3.Connection, activity_id: int, start_m: float | None = None, end_m: float | None = None
) -> dict[str, object]:
    if not streams.has_cached_streams(activity_id):
        raise TerrainDataError(f"No cached streams for activity {activity_id}")
    df = streams.get_stream_dataframe(activity_id)
    track = df.dropna(subset=["distance_m", "lat", "lng"])
    if track.empty:
        raise TerrainDataError(f"Activity {activity_id} has no GPS stream")
    xp = track["distance_m"].astype(float).to_numpy()
    total = float(xp[-1])
    start = min(max(start_m if start_m is not None else 0.0, 0.0), total)
    end = min(max(end_m if end_m is not None else total, 0.0), total)
    if end <= start:
        raise TerrainDataError(f"Empty stretch: start_m {start:.0f} to end_m {end:.0f} of a {total:.0f} m run")

    dist = np.linspace(start, end, max(2, math.ceil((end - start) / STEP_M) + 1))
    lat = np.interp(dist, xp, track["lat"].astype(float).to_numpy())
    lng = np.interp(dist, xp, track["lng"].astype(float).to_numpy())
    recorded: list[float | None] = []
    altitude = df.dropna(subset=["distance_m", "altitude_m"])
    if len(altitude) >= 2:
        recorded = list(np.interp(dist, altitude["distance_m"].astype(float).to_numpy(), altitude["altitude_m"].astype(float).to_numpy()).tolist())
    mapped = elevation.sampler_for(conn).sample(list(zip(lat.tolist(), lng.tolist())))

    area = _covering_area(conn, float(lat[0]), float(lng[0]))
    pick = np.unique(np.linspace(0, len(dist) - 1, MAX_POINTS).round().astype(int))
    result: dict[str, object] = {
        "activity_id": activity_id,
        "start_m": round(start, 1),
        "end_m": round(end, 1),
        "length_m": round(end - start, 1),
        "start": {"lat": round(float(lat[0]), 6), "lng": round(float(lng[0]), 6)},
        "end": {"lat": round(float(lat[-1]), 6), "lng": round(float(lng[-1]), 6)},
        "recorded_altitude": _profile(recorded, dist, f"recorded altitude stream, {SMOOTHING_SAMPLES}-sample moving average over {STEP_M:.0f} m steps"),
        "map_elevation": _profile(mapped, dist, f"cached map elevation tiles, {SMOOTHING_SAMPLES}-sample moving average over {STEP_M:.0f} m steps"),
        "points": [
            {
                "dist_m": round(float(dist[i]), 1),
                "lat": round(float(lat[i]), 6),
                "lng": round(float(lng[i]), 6),
                "recorded_altitude_m": round(recorded[i], 1) if recorded else None,
                "map_elevation_m": round(mapped[i], 1) if mapped[i] is not None else None,
            }
            for i in pick
        ],
        "area": area["label"] if area else None,
        "ways": _ways(conn, area["area_id"], dist, lat, lng) if area else [],
    }
    if area is None:
        result["ways_note"] = "No imported area covers this run, so no way names are available."
    return result
