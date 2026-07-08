"""Infer run_type for activities the athlete never tagged in Strava.

Strava's workout_type 0 means unset; sync maps it to run_type='easy'. This module
proposes a type for those rows only — an explicit tag always wins (see
db.EFFECTIVE_RUN_TYPE_SQL). Pure and deterministic; fully recomputed each sync.

Rules in priority order (first match wins): workout (name), race (distance bucket +
agreeing name), long_run (name, or long vs. current volume AND above the athlete's
learned floor). Trailing windows are relative to each activity's own date, not today,
so old rows are judged against the norms of their era.
"""

import re
import sqlite3
import statistics
from bisect import bisect_left
from datetime import date, datetime
from typing_extensions import TypedDict

from . import db
from .classifier import classify_workout
from .races import classify_race_distance

MILES_TO_METERS = 1609.34

# Workout inference is name-based only: a max/avg speed-spread signal can't separate
# workouts from ordinary runs (max_speed is an instantaneous GPS value).
REP_SCHEME_RE = re.compile(r"(?i)\b\d+\s*x\s*\d+\b")  # "5x800", "6 x 400"

# Generic race words count for any distance bucket; distance tokens only when the
# activity sits in the matching bucket (a "5k" mention on a 10K-distance run is an
# effort note). Bare numeric tokens ("13.1") proved too ambiguous to keep.
RACE_GENERIC_RE = re.compile(r"(?i)\b(race|parkrun|relay|champs?|championship)\b")
RACE_DISTANCE_TOKENS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b5k\b"), "5K"),
    (re.compile(r"(?i)\b10k\b"), "10K"),
    (re.compile(r"(?i)\b10 ?miler?\b"), "10M"),
    (re.compile(r"(?i)\bhalf\b"), "half"),
    (re.compile(r"(?i)\bmarathon\b"), "marathon"),
    (re.compile(r"(?i)\b50k\b"), "50K"),
]

# A name that says "long run" is the athlete's own word for it; the 70-minute
# duration gate still applies (screens out "Not long run"-style negations too).
LONG_RUN_NAME_RE = re.compile(r"(?i)\blong\s*run\b")

LONG_RUN_MIN_S = 70 * 60
LONG_RUN_FACTOR = 1.4
LONG_RUN_RECENT_DAYS = 90
LONG_RUN_NORM_DAYS = 730
LONG_RUN_MIN_TAGGED = 10
LONG_RUN_P95_FRACTION = 0.7
# Tagged long runs skew toward peak-phase distances, so their raw P25 overshoots
# the athlete's true long-run floor; scale it down to admit early-build and taper
# long runs.
LONG_RUN_TAGGED_P25_SCALE = 0.9


class ActivityForInference(TypedDict):
    activity_id: int
    name: str | None
    start_date: str | None
    workout_type: int
    distance_m: float | None
    moving_time_s: int | None
    average_speed_mps: float | None
    max_speed_mps: float | None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (0-100), matching numpy's default 'linear' method."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def infer_run_types(
    activities: list[ActivityForInference], *, long_run_floor_m: float | None = None
) -> dict[int, str]:
    """
    Infer run_type for workout_type == 0 rows in `activities` (must be chronologically
    sorted by start_date). Returns {activity_id: inferred_type} only for rows where a
    rule fired; rows with no match get no entry (treated as easy by the caller).

    long_run_floor_m, when given, replaces the learned tagged-P25/P95 floor entirely
    (an athlete's explicit override); the relative and duration checks still apply.
    """
    dated = [(d, a) for a in activities if (d := _parse_date(a["start_date"])) is not None]
    dated.sort(key=lambda pair: pair[0])

    # Parallel arrays (sorted by date) for trailing-window lookups via bisect.
    all_ordinals: list[int] = []
    all_distances: list[float] = []
    tagged_ordinals: list[int] = []  # Strava-tagged long runs (workout_type == 2) only
    tagged_distances: list[float] = []
    for d, a in dated:
        dist = a["distance_m"]
        if dist is None:
            continue
        ordv = d.toordinal()
        all_ordinals.append(ordv)
        all_distances.append(float(dist))
        if a["workout_type"] == 2:
            tagged_ordinals.append(ordv)
            tagged_distances.append(float(dist))

    def window(ordinals: list[int], distances: list[float], ordv: int, days: int) -> list[float]:
        lo = bisect_left(ordinals, ordv - days)
        hi = bisect_left(ordinals, ordv)  # exclusive of the activity's own day
        return distances[lo:hi]

    result: dict[int, str] = {}
    for d, a in dated:
        if a["workout_type"] != 0:
            continue

        name = a["name"] or ""
        distance_m = a["distance_m"]
        moving_time_s = a["moving_time_s"]
        ordv = d.toordinal()

        # Workout before race: rep-scheme names often cite a race distance as an
        # effort target.
        if classify_workout(name) is not None or REP_SCHEME_RE.search(name):
            result[a["activity_id"]] = "workout"
            continue

        bucket = classify_race_distance(distance_m)
        if bucket is not None and (
            RACE_GENERIC_RE.search(name)
            or any(pat.search(name) and cat == bucket for pat, cat in RACE_DISTANCE_TOKENS)
        ):
            result[a["activity_id"]] = "race"
            continue

        if moving_time_s is not None and moving_time_s >= LONG_RUN_MIN_S and LONG_RUN_NAME_RE.search(name):
            result[a["activity_id"]] = "long_run"
            continue

        if moving_time_s is not None and moving_time_s >= LONG_RUN_MIN_S and distance_m is not None:
            recent_window = window(all_ordinals, all_distances, ordv, LONG_RUN_RECENT_DAYS)
            if recent_window:
                median_recent = statistics.median(recent_window)
                if median_recent > 0 and distance_m >= LONG_RUN_FACTOR * median_recent:
                    floor: float | None
                    if long_run_floor_m is not None:
                        floor = long_run_floor_m
                    else:
                        # Widen the tagged lookback until enough exist — a long-run
                        # standard doesn't shrink during a training lapse.
                        tagged_window = window(tagged_ordinals, tagged_distances, ordv, LONG_RUN_NORM_DAYS)
                        if len(tagged_window) < LONG_RUN_MIN_TAGGED:
                            tagged_window = window(tagged_ordinals, tagged_distances, ordv, LONG_RUN_NORM_DAYS * 2)
                        if len(tagged_window) < LONG_RUN_MIN_TAGGED:
                            tagged_window = tagged_distances[: bisect_left(tagged_ordinals, ordv)]
                        if len(tagged_window) >= LONG_RUN_MIN_TAGGED:
                            floor = LONG_RUN_TAGGED_P25_SCALE * _percentile(tagged_window, 25)
                        else:
                            norm_window = window(all_ordinals, all_distances, ordv, LONG_RUN_NORM_DAYS)
                            floor = LONG_RUN_P95_FRACTION * _percentile(norm_window, 95) if norm_window else None
                    if floor is not None and distance_m >= floor:
                        result[a["activity_id"]] = "long_run"
                        continue

    return result


def apply_inference(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Full recompute of run_type_inferred for every Run/TrailRun/VirtualRun activity.
    Honors the athlete's long_run_floor_miles override (see db.get_athlete) when set.
    Idempotent: always clears workout_type == 0 rows first, then re-derives from
    scratch, so re-running after a threshold tweak (or a fresh sync) never leaves
    stale values around. Returns counts of inferred rows per inferred type.
    """
    rows = conn.execute("""
        SELECT activity_id, name, start_date, workout_type, distance_m,
               moving_time_s, average_speed_mps, max_speed_mps
        FROM activities
        WHERE sport_type IN ('Run', 'TrailRun', 'VirtualRun')
        ORDER BY start_date
    """).fetchall()

    activities: list[ActivityForInference] = [
        {
            "activity_id": int(row["activity_id"]),
            "name": row["name"],
            "start_date": row["start_date"],
            "workout_type": int(row["workout_type"]) if row["workout_type"] is not None else 0,
            "distance_m": float(row["distance_m"]) if row["distance_m"] is not None else None,
            "moving_time_s": int(row["moving_time_s"]) if row["moving_time_s"] is not None else None,
            "average_speed_mps": float(row["average_speed_mps"]) if row["average_speed_mps"] is not None else None,
            "max_speed_mps": float(row["max_speed_mps"]) if row["max_speed_mps"] is not None else None,
        }
        for row in rows
    ]

    athlete = db.get_athlete(conn)
    long_run_floor_m = (
        athlete["long_run_floor_miles"] * MILES_TO_METERS
        if athlete and athlete["long_run_floor_miles"] is not None
        else None
    )

    inferred = infer_run_types(activities, long_run_floor_m=long_run_floor_m)

    conn.execute("UPDATE activities SET run_type_inferred = NULL WHERE workout_type = 0")
    conn.executemany(
        "UPDATE activities SET run_type_inferred = ? WHERE activity_id = ?",
        [(run_type, activity_id) for activity_id, run_type in inferred.items()],
    )
    conn.commit()

    counts: dict[str, int] = {}
    for run_type in inferred.values():
        counts[run_type] = counts.get(run_type, 0) + 1
    return counts
