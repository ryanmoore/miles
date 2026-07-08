"""Per-build pace claims split by workout intent (5K / LT-tempo / MP), derived
from classified work laps — replaces the old single "workout pace" average,
which blended reps of wildly different intent into a meaningless number.

Each work lap is classified by its pace relative to a 5K-equivalent baseline
(the fitness estimate as of build_start), since duration alone can't tell a
mile-pace rep from a 5K-pace rep of similar length. Activities explicitly
named/tagged MP or LT/tempo/threshold override the pace-based classification
for all their work laps. When no baseline estimate is computable that far
back, the whole build falls back to the old per-activity duration bucketing.
"""

import re
import sqlite3
import statistics
from datetime import date
from typing_extensions import TypedDict

from .db import effective_run_type_sql
from .fitness import estimate_fitness

_MP_RE = re.compile(r"\bmp\b|marathon pace", re.IGNORECASE)
_LT_RE = re.compile(r"\blt\b|tempo|threshold", re.IGNORECASE)

_METERS_PER_MILE = 1609.344

# Pace bands: ratio of a work lap's pace to the 5K-equivalent baseline pace
# (fitness estimate as of build_start). Tunable relative to that baseline.
_SPEED_MAX_RATIO = 0.95  # faster than this -> mile-pace/speed rep, excluded
_5K_MAX_RATIO = 1.06  # [_SPEED_MAX_RATIO, _5K_MAX_RATIO) -> '5k'
_LT_MAX_RATIO = 1.14  # [_5K_MAX_RATIO, _LT_MAX_RATIO) -> 'lt'
_MP_MAX_RATIO = 1.28  # [_LT_MAX_RATIO, _MP_MAX_RATIO] -> 'mp'; beyond -> excluded
_5K_MIN_DISTANCE_M = 600  # shorter reps in the 5K pace band are strides, not 5K work

_BUCKET_KEYS = ("5k", "lt", "mp")


class PaceClaim(TypedDict):
    pace_min_per_mile: float
    workouts: int


class _LapRow(TypedDict):
    activity_id: int
    workout_label: str | None
    name: str | None
    moving_time_s: float
    distance_m: float


def _bucket_by_text(text: str | None) -> str | None:
    if not text:
        return None
    if _MP_RE.search(text):
        return "mp"
    if _LT_RE.search(text):
        return "lt"
    return None


def _fetch_work_laps(conn: sqlite3.Connection, build_start: str, race_date: str) -> list[_LapRow]:
    effective_run_type = effective_run_type_sql("a")
    rows = conn.execute(f"""
        SELECT a.activity_id, a.workout_label, a.name, l.moving_time_s, l.distance_m
        FROM activities a
        JOIN laps l ON l.activity_id = a.activity_id
        WHERE {effective_run_type} = 'workout'
          AND DATE(a.start_date) >= ? AND DATE(a.start_date) < ?
          AND l.lap_type = 'work'
          AND l.moving_time_s >= 45 AND l.distance_m >= 200
    """, (build_start, race_date)).fetchall()
    return [
        _LapRow(
            activity_id=r["activity_id"],
            workout_label=r["workout_label"],
            name=r["name"],
            moving_time_s=r["moving_time_s"],
            distance_m=r["distance_m"],
        )
        for r in rows
    ]


def _finalize(
    buckets: dict[str, list[tuple[float, float]]], workout_ids: dict[str, set[int]]
) -> dict[str, PaceClaim | None]:
    out: dict[str, PaceClaim | None] = {}
    for key in _BUCKET_KEYS:
        items = buckets[key]
        if not items:
            out[key] = None
            continue
        total_dist = sum(d for _, d in items)
        weighted_pace = sum(p * d for p, d in items) / total_dist
        out[key] = PaceClaim(pace_min_per_mile=round(weighted_pace, 2), workouts=len(workout_ids[key]))
    return out


def _bucket_by_duration(rows: list[_LapRow]) -> dict[str, PaceClaim | None]:
    """Fallback when no 5K-pace baseline is available: bucket whole activities by
    keyword override, else median work-lap duration (the original heuristic)."""
    by_activity: dict[int, dict[str, object]] = {}
    for r in rows:
        entry = by_activity.setdefault(r["activity_id"], {
            "workout_label": r["workout_label"],
            "name": r["name"],
            "times": [],
            "dists": [],
        })
        entry["times"].append(r["moving_time_s"])  # type: ignore[attr-defined]
        entry["dists"].append(r["distance_m"])  # type: ignore[attr-defined]

    buckets: dict[str, list[tuple[float, float]]] = {"5k": [], "lt": [], "mp": []}
    workout_ids: dict[str, set[int]] = {"5k": set(), "lt": set(), "mp": set()}
    for activity_id, entry in by_activity.items():
        times = entry["times"]
        dists = entry["dists"]
        assert isinstance(times, list) and isinstance(dists, list)
        total_time_s: float = sum(times)
        total_dist_m: float = sum(dists)
        if total_dist_m <= 0:
            continue
        median_work_s = statistics.median(times)
        pace = (total_time_s / 60) / (total_dist_m / _METERS_PER_MILE)
        label = entry["workout_label"]
        name = entry["name"]
        bucket = (
            _bucket_by_text(label if isinstance(label, str) else None)
            or _bucket_by_text(name if isinstance(name, str) else None)
            or ("5k" if median_work_s <= 210 else "lt" if median_work_s <= 600 else "mp")
        )
        buckets[bucket].append((pace, total_dist_m))
        workout_ids[bucket].add(activity_id)

    return _finalize(buckets, workout_ids)


def _bucket_by_pace(rows: list[_LapRow], baseline_5k: float) -> dict[str, PaceClaim | None]:
    """Per-lap classification by pace ratio to the 5K baseline, with an
    activity-level keyword override (workout_label, then name) for MP/LT
    workouts that sends all of that activity's work laps to one bucket."""
    by_activity: dict[int, list[_LapRow]] = {}
    for r in rows:
        by_activity.setdefault(r["activity_id"], []).append(r)

    buckets: dict[str, list[tuple[float, float]]] = {"5k": [], "lt": [], "mp": []}
    workout_ids: dict[str, set[int]] = {"5k": set(), "lt": set(), "mp": set()}
    for activity_id, laps in by_activity.items():
        override = _bucket_by_text(laps[0]["workout_label"]) or _bucket_by_text(laps[0]["name"])
        for lap in laps:
            distance_m = lap["distance_m"]
            if distance_m <= 0:
                continue
            pace = (lap["moving_time_s"] / 60) / (distance_m / _METERS_PER_MILE)
            if override is not None:
                bucket = override
            else:
                ratio = pace / baseline_5k
                if ratio < _SPEED_MAX_RATIO:
                    continue  # mile-pace/speed rep, not aerobic work
                elif ratio < _5K_MAX_RATIO:
                    if distance_m < _5K_MIN_DISTANCE_M:
                        continue  # short fast rep: a stride, not 5K work
                    bucket = "5k"
                elif ratio < _LT_MAX_RATIO:
                    bucket = "lt"
                elif ratio <= _MP_MAX_RATIO:
                    bucket = "mp"
                else:
                    continue  # much slower than MP: recovery-ish, not a claim
            buckets[bucket].append((pace, distance_m))
            workout_ids[bucket].add(activity_id)

    return _finalize(buckets, workout_ids)


def pace_claims(conn: sqlite3.Connection, build_start: str, race_date: str) -> dict[str, PaceClaim | None]:
    """Distance-weighted work-lap pace per intent bucket ('5k', 'lt', 'mp') for
    activities in [build_start, race_date). None where no supporting laps exist.

    Classifies each work lap by pace relative to a 5K-equivalent baseline (the
    fitness estimate as of build_start); falls back to whole-activity duration
    bucketing (_bucket_by_duration) when no estimate is computable that far back.
    """
    rows = _fetch_work_laps(conn, build_start, race_date)
    if not rows:
        return {"5k": None, "lt": None, "mp": None}

    est = estimate_fitness(conn, date.fromisoformat(build_start))
    if est is None:
        return _bucket_by_duration(rows)

    baseline_5k = est["predicted"]["5K"]
    return _bucket_by_pace(rows, baseline_5k)
