"""
Dated fitness estimate: predicted race paces as of a date, from the best
available signal in a trailing window — races, classified workout laps, or a
training-pace envelope — with confidence and evidence attached.
"""

import sqlite3
import statistics
from datetime import date, timedelta
from typing import Literal, NotRequired, TypedDict

from .db import effective_run_type_sql, get_athlete
from .races import NOMINAL_METERS, classify_race_distance, riegel_time

WINDOW_DAYS = 180
HALF_LIFE_DAYS = 90  # recency weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
STALE_RACE_DAYS = 120
ENVELOPE_RACE_FACTOR = 0.97
WORKOUT_ANCHOR_MIN_WORK_S = 12 * 60
WORKOUT_ANCHOR_MIN_REP_S = 150  # sprint/stride reps outpace any race pace; never anchor
ELEV_GAIN_PER_M_MAX = 0.019  # ~100 ft/mi — hills corrupt pace signals

# Race-effort bands: effort_ratio = actual/predicted pace (>1 = slower than predicted).
# Soft bands widen the "raced"/"hard" cutoffs for sub-race-signal confidence tiers,
# where the predicted pace is itself a floor rather than a point estimate.
EFFORT_RACED_MAX = 1.03
EFFORT_HARD_MAX = 1.08
EFFORT_RACED_MAX_SOFT = 1.05
EFFORT_HARD_MAX_SOFT = 1.12
HR_CEILING_PCT = 95
# HR fraction that corroborates a raced effort — lower for half-and-longer races, where
# average HR for a sustained effort runs lower than it does over a 5K/10K.
HR_RACED_FRACTION_SHORT = 0.93  # shorter than half
HR_RACED_FRACTION_LONG = 0.86  # half and longer
HR_EASY_FRACTION = 0.80

Effort = Literal["raced", "hard", "casual"]
_SOFT_CONFIDENCE = ("medium-low", "low")

MILE_M = 1609.34
_RUN_SPORTS = ("Run", "TrailRun", "VirtualRun")

# Projection targets: the four headline distances plus the two zone anchors.
_PREDICTED_KEYS = ("5K", "10K", "half", "marathon")
_PROJECTION_METERS: dict[str, float] = {
    "5K": NOMINAL_METERS["5K"],
    "10K": NOMINAL_METERS["10K"],
    "half": NOMINAL_METERS["half"],
    "marathon": NOMINAL_METERS["marathon"],
    "15K": NOMINAL_METERS["15K"],
    "mile": MILE_M,
}

Confidence = Literal["high", "medium", "medium-low", "low"]


class Source(TypedDict):
    tier: int
    activity_id: int
    date: str
    name: str | None
    detail: str


class FitnessEstimate(TypedDict):
    as_of: str
    confidence: Confidence
    predicted: dict[str, float]
    zones: dict[str, float | str]
    sources: list[Source]
    note: NotRequired[str]


class _Candidate(TypedDict):
    tier: int
    confidence: Confidence
    paces: dict[str, float]  # decimal min/mi keyed by _PROJECTION_METERS
    sources: list[Source]
    newest_date: str


def _pace_str(v: float) -> str:
    mins = int(v)
    secs = round((v - mins) * 60)
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}"


def _project(time_s: float, from_m: float) -> dict[str, float]:
    """Riegel-project a performance to every target; paces in decimal min/mi."""
    out: dict[str, float] = {}
    for key, to_m in _PROJECTION_METERS.items():
        t = riegel_time(time_s, from_m, to_m)
        out[key] = (t / 60.0) / (to_m / MILE_M)
    return out


def _sport_filter(sports: tuple[str, ...]) -> tuple[str, list[str]]:
    placeholders = ",".join("?" * len(sports))
    return f"sport_type IN ({placeholders})", list(sports)


def _tier1_races(conn: sqlite3.Connection, as_of: date, *, exclude_casual: bool = False) -> _Candidate | None:
    """Races in-window with a known distance category and finish time, combined
    by recency-weighted average in pace space."""
    sport_clause, params = _sport_filter(_RUN_SPORTS)
    effective = effective_run_type_sql()
    window_start = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
    casual_clause = "AND (race_effort IS NULL OR race_effort != 'casual')" if exclude_casual else ""
    rows = conn.execute(f"""
        SELECT activity_id, name, DATE(start_date) AS date, distance_m, moving_time_s
        FROM activities
        WHERE {sport_clause} AND {effective} = 'race'
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
          {casual_clause}
        ORDER BY date
    """, params + [window_start, as_of.isoformat()]).fetchall()

    weighted: list[tuple[float, dict[str, float]]] = []
    sources: list[Source] = []
    newest_date: str | None = None
    for r in rows:
        category = classify_race_distance(r["distance_m"])
        if category is None or r["moving_time_s"] is None:
            continue
        race_date: str = r["date"]
        age_days = (as_of - date.fromisoformat(race_date)).days
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        # Finish time is taken at the category's nominal distance, not GPS distance.
        nominal_m = NOMINAL_METERS[category]
        finish_s = float(r["moving_time_s"])
        paces = _project(finish_s, nominal_m)
        weighted.append((weight, paces))
        race_pace = (finish_s / 60.0) / (nominal_m / MILE_M)
        sources.append({
            "tier": 1,
            "activity_id": int(r["activity_id"]),
            "date": race_date,
            "name": r["name"],
            "detail": (
                f"{category} race at {_pace_str(race_pace)}/mi, "
                f"{age_days}d old, weight {weight:.2f}"
            ),
        })
        newest_date = race_date if newest_date is None else max(newest_date, race_date)

    if not weighted or newest_date is None:
        return None

    total_w = sum(w for w, _ in weighted)
    combined = {
        key: sum(w * p[key] for w, p in weighted) / total_w
        for key in _PROJECTION_METERS
    }
    newest_age = (as_of - date.fromisoformat(newest_date)).days
    confidence: Confidence = "high" if newest_age <= 90 else "medium"
    return {
        "tier": 1,
        "confidence": confidence,
        "paces": combined,
        "sources": sources,
        "newest_date": newest_date,
    }


def _tier2_workout_laps(conn: sqlite3.Connection, as_of: date) -> _Candidate | None:
    """Fastest in-window workout session (by median work-lap pace) with enough
    work volume and long-enough reps, its median treated as 5K race pace."""
    sport_clause, params = _sport_filter(_RUN_SPORTS)
    effective = effective_run_type_sql()
    window_start = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = conn.execute(f"""
        SELECT activity_id, name, DATE(start_date) AS date,
               total_elevation_gain_m, distance_m
        FROM activities
        WHERE {sport_clause} AND {effective} = 'workout'
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
    """, params + [window_start, as_of.isoformat()]).fetchall()

    best: tuple[float, int, sqlite3.Row] | None = None  # (median_pace, work_s, row)
    for r in rows:
        distance_m = r["distance_m"]
        gain_m = r["total_elevation_gain_m"] or 0.0
        if distance_m is None or distance_m <= 0 or gain_m / distance_m > ELEV_GAIN_PER_M_MAX:
            continue
        laps = conn.execute("""
            SELECT moving_time_s, average_speed_mps FROM laps
            WHERE activity_id = ? AND lap_type = 'work'
              AND average_speed_mps IS NOT NULL AND average_speed_mps > 0
        """, [r["activity_id"]]).fetchall()
        if not laps:
            continue
        work_s = sum(int(lap["moving_time_s"] or 0) for lap in laps)
        if work_s < WORKOUT_ANCHOR_MIN_WORK_S:
            continue
        median_rep_s = statistics.median(int(lap["moving_time_s"] or 0) for lap in laps)
        if median_rep_s < WORKOUT_ANCHOR_MIN_REP_S:
            continue
        median_pace = statistics.median(26.8224 / float(lap["average_speed_mps"]) for lap in laps)
        if best is None or median_pace < best[0]:
            best = (median_pace, work_s, r)

    if best is None:
        return None
    median_pace, work_s, row = best
    anchor_time_s = median_pace * (NOMINAL_METERS["5K"] / MILE_M) * 60.0
    session_date: str = row["date"]
    return {
        "tier": 2,
        "confidence": "medium-low",
        "paces": _project(anchor_time_s, NOMINAL_METERS["5K"]),
        "sources": [{
            "tier": 2,
            "activity_id": int(row["activity_id"]),
            "date": session_date,
            "name": row["name"],
            "detail": (
                f"fastest workout anchor: median work pace {_pace_str(median_pace)}/mi "
                f"over {work_s // 60} min of work laps, treated as 5K race pace"
            ),
        }],
        "newest_date": session_date,
    }


def _tier3_envelope(conn: sqlite3.Connection, as_of: date) -> _Candidate | None:
    """Fastest sustained (20-45 min) non-trail run in-window; its pace scaled by
    ENVELOPE_RACE_FACTOR is treated as 10K race pace. Explicitly a floor."""
    sports = tuple(s for s in _RUN_SPORTS if s != "TrailRun")
    sport_clause, params = _sport_filter(sports)
    window_start = (as_of - timedelta(days=WINDOW_DAYS)).isoformat()
    row = conn.execute(f"""
        SELECT activity_id, name, DATE(start_date) AS date,
               average_speed_mps, moving_time_s
        FROM activities
        WHERE {sport_clause}
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
          AND moving_time_s BETWEEN 1200 AND 2700
          AND average_speed_mps IS NOT NULL AND average_speed_mps > 0
          AND distance_m IS NOT NULL AND distance_m > 0
          AND COALESCE(total_elevation_gain_m, 0) / distance_m <= ?
        ORDER BY average_speed_mps DESC
        LIMIT 1
    """, params + [window_start, as_of.isoformat(), ELEV_GAIN_PER_M_MAX]).fetchone()

    if row is None:
        return None
    run_pace = 26.8224 / float(row["average_speed_mps"])
    race_pace_10k = run_pace * ENVELOPE_RACE_FACTOR
    anchor_time_s = race_pace_10k * (NOMINAL_METERS["10K"] / MILE_M) * 60.0
    run_date: str = row["date"]
    return {
        "tier": 3,
        "confidence": "low",
        "paces": _project(anchor_time_s, NOMINAL_METERS["10K"]),
        "sources": [{
            "tier": 3,
            "activity_id": int(row["activity_id"]),
            "date": run_date,
            "name": row["name"],
            "detail": (
                f"fastest sustained run in window ({int(row['moving_time_s']) // 60} min at "
                f"{_pace_str(run_pace)}/mi) x {ENVELOPE_RACE_FACTOR} — a floor from training pace, "
                f"not a race result"
            ),
        }],
        "newest_date": run_date,
    }


def estimate_fitness(
    conn: sqlite3.Connection, as_of: date, *, exclude_casual: bool = False
) -> FitnessEstimate | None:
    """Best-available fitness estimate as of a date; None when the trailing
    window has no usable signal at all. exclude_casual drops tier-1 races whose
    persisted race_effort is 'casual' (the derive-layer refinement pass)."""
    tier1 = _tier1_races(conn, as_of, exclude_casual=exclude_casual)
    tier2 = _tier2_workout_laps(conn, as_of)
    tier3 = _tier3_envelope(conn, as_of)
    if tier1 is None and tier2 is None and tier3 is None:
        return None

    chosen: _Candidate
    note: str | None = None
    extra_sources: list[Source] = []

    lower = min(
        (c for c in (tier2, tier3) if c is not None),
        key=lambda c: c["paces"]["10K"],
        default=None,
    )
    if tier1 is not None:
        newest_age = (as_of - date.fromisoformat(tier1["newest_date"])).days
        if newest_age <= STALE_RACE_DAYS or lower is None or lower["paces"]["10K"] >= tier1["paces"]["10K"]:
            chosen = tier1
        else:
            # Stale race conflicts with a faster lower-tier signal: trust the latter.
            chosen = lower
            extra_sources = tier1["sources"]
            stale = tier1["sources"][-1]
            note = (
                f"Most recent race ({stale['name']}, {stale['date']}) is {newest_age} days old "
                f"(over {STALE_RACE_DAYS}); a tier-{lower['tier']} training signal "
                f"({lower['sources'][0]['name']}, {lower['sources'][0]['date']}) predicts a faster 10K "
                f"({_pace_str(lower['paces']['10K'])}/mi vs {_pace_str(tier1['paces']['10K'])}/mi), "
                f"so the estimate uses the training signal."
            )
    else:
        assert lower is not None
        chosen = lower

    paces = chosen["paces"]
    marathon_pace = paces["marathon"]
    zones: dict[str, float | str] = {
        "easy_range": f"{_pace_str(marathon_pace + 1.0)}–{_pace_str(marathon_pace + 1.75)}/mi",
        "marathon": round(marathon_pace, 2),
        "threshold": round(paces["15K"], 2),
        "interval": round(paces["5K"], 2),
        "repetition": round(paces["mile"], 2),
    }
    estimate: FitnessEstimate = {
        "as_of": as_of.isoformat(),
        "confidence": chosen["confidence"],
        "predicted": {k: round(paces[k], 2) for k in _PREDICTED_KEYS},
        "zones": zones,
        "sources": chosen["sources"] + extra_sources,
    }
    if note is not None:
        estimate["note"] = note
    return estimate


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (0-100)."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def hr_ceiling(conn: sqlite3.Connection, as_of: date) -> float | None:
    """Athlete's configured max_hr when set; else the HR_CEILING_PCT percentile of
    max_heartrate over runs in the trailing year before as_of, widening to all
    history before as_of when fewer than 10 samples. None when there's still no signal."""
    athlete = get_athlete(conn)
    if athlete is not None and athlete["max_hr"] is not None:
        return float(athlete["max_hr"])

    sport_clause, params = _sport_filter(_RUN_SPORTS)
    window_start = (as_of - timedelta(days=365)).isoformat()
    rows = conn.execute(f"""
        SELECT max_heartrate FROM activities
        WHERE {sport_clause} AND max_heartrate IS NOT NULL
          AND DATE(start_date) >= ? AND DATE(start_date) < ?
    """, params + [window_start, as_of.isoformat()]).fetchall()
    values = [float(r["max_heartrate"]) for r in rows]

    if len(values) < 10:
        rows = conn.execute(f"""
            SELECT max_heartrate FROM activities
            WHERE {sport_clause} AND max_heartrate IS NOT NULL
              AND DATE(start_date) < ?
        """, params + [as_of.isoformat()]).fetchall()
        values = [float(r["max_heartrate"]) for r in rows]

    if not values:
        return None
    return _percentile(values, HR_CEILING_PCT)


def hr_raced_fraction(category: str) -> float:
    """HR-ceiling fraction that counts as corroborating a raced effort for this race
    category — lower for half-and-longer races (see HR_RACED_FRACTION_LONG)."""
    is_long = NOMINAL_METERS[category] >= NOMINAL_METERS["half"]
    return HR_RACED_FRACTION_LONG if is_long else HR_RACED_FRACTION_SHORT


def classify_race_effort(
    actual_pace_min_mi: float,
    predicted_pace_min_mi: float,
    avg_hr: float | None,
    hr_ceiling: float | None,
    confidence: Confidence,
    category: str,
) -> tuple[Effort, float]:
    """How hard a race was actually run vs. the fitness estimate at the time.
    effort_ratio = actual/predicted (>1 = slower than predicted). Bands widen for
    medium-low/low confidence estimates, which are floors, not point estimates.
    HR corroborates when both avg_hr and hr_ceiling are present: near-max HR promotes
    a casual-by-pace race to hard (fought hard on a bad day) — the bar is
    hr_raced_fraction(category), lower for half-and-longer races; low HR demotes a
    raced-by-pace race to hard (fast but not truly maxed), using the universal
    HR_EASY_FRACTION regardless of distance."""
    ratio = actual_pace_min_mi / predicted_pace_min_mi
    soft = confidence in _SOFT_CONFIDENCE
    raced_max = EFFORT_RACED_MAX_SOFT if soft else EFFORT_RACED_MAX
    hard_max = EFFORT_HARD_MAX_SOFT if soft else EFFORT_HARD_MAX

    effort: Effort
    if ratio <= raced_max:
        effort = "raced"
    elif ratio <= hard_max:
        effort = "hard"
    else:
        effort = "casual"

    if avg_hr is not None and hr_ceiling is not None and hr_ceiling > 0:
        if effort == "casual" and avg_hr >= hr_raced_fraction(category) * hr_ceiling:
            effort = "hard"
        elif effort == "raced" and avg_hr < HR_EASY_FRACTION * hr_ceiling:
            effort = "hard"

    return effort, ratio
