"""
Adherence engine: judges each week of the active plan that the sync has
fully covered (see the unsynced-week guard below) against its contemporaneous
plan version (see plan.current_version_for_week — a week's Monday
floor-rules to version 1 even if authored later). The week is the contract;
day placement inside it never matters here.

Design (see adr/0001-training-plans-as-versioned-ground-truth.md):
  - Mileage band (WEEK_ON_LO/HI, WEEK_CLOSE_LO/HI) and workout-count band
    (exact-or-over on, one short close, two-plus short off) are scored
    independently per week; the week's overall `band` is the worse of the
    two (_worse_band) — a mileage-close week that hit its workouts is still
    a normal week, but a workout-count miss on an on-mileage week is a real
    week-level shortfall and should read that way.
  - Mileage ratio for a range week (target_miles_hi set) is actual vs the
    nearest bound — inside [target_miles, target_miles_hi] scores 1.0, the
    band follows from that ratio same as a point week. A week with neither
    target_miles nor target_miles_hi is deliberately unspecified: mileage_
    ratio is NULL and mileage_band never worsens the week's overall band —
    only the workout-count band judges it.
  - Long run: satisfied by any single run >= LONG_RUN_ON_RATIO of either
    target_long_run_miles or target_long_run_minutes (moving_time), any day
    of the week — a week carrying both targets is satisfied by either one.
    Weeks with neither target are excluded from that judgment entirely
    (long_run_done = None), not counted as met.
  - Trail days (plan_days.terrain = 'trail') never contribute to
    workout_pace_delta_s — grade makes road pace bands meaningless. The
    activity still counts toward the week's mileage and workout-count bands;
    only its pace contribution to a matched workout slot is suppressed.
  - Strength: actual_strength_days counts distinct dates with a synced
    STRENGTH_SPORT_TYPES activity that week — displayed alongside
    target_strength_days, never scored (no band, no flag; gym logging in
    Strava is too inconsistent to judge silence).
  - Workout pace: synced workout-type activities in the week are matched to
    planned workout slots by count (date order), preferring an exact
    keyword match (classify_workout's label vs the planned slot's title)
    when one exists. workout_pace_delta_s is the distance-weighted average,
    across matched pairs with both a frozen target range and a computed
    actual pace (trail-terrain slots excluded), of each pair's signed
    seconds/mile *outside* the target range expanded by +/- PACE_TOLERANCE
    (0 when the actual pace falls within the tolerance-expanded range) —
    this reads as "how far off, if at all" rather than always-nonzero noise
    around a floating midpoint.
  - Unsynced-week guard: a week whose Sunday falls on or after the sync's
    coverage date (meta.last_sync_at, DATE-truncated) gets no adherence row
    at all — absence, not a score — because the sync hasn't seen it yet.
    Pre-v2 DBs with no last_sync_at stamped fall back to scoring every week
    that has already ended as of today, matching the original behavior.
  - Flags fire on patterns only: a maximal run of 2+ consecutive qualifying
    weeks gets exactly ONE flag object, attached to the run's last (most
    recent) week — never one flag per week in the run. A single off week is
    never flagged. Four flag types: mileage_off_low, mileage_off_high,
    workout_count_short (close-or-off, i.e. any shortfall), long_run_missed
    (over the subsequence of weeks that had a long-run target).

Pure scoring lives in score_week/_build_flags (plain data in, scored rows
out); compute_plan_adherence is the thin conn-taking orchestrator that
gathers plan weeks/days and synced activities/laps, matching derive.py's
"full recompute" style — callers DELETE existing rows and re-INSERT what
this returns.
"""

import json
import sqlite3
from datetime import date, timedelta

from typing_extensions import TypedDict

from . import db
from .classifier import classify_workout
from .db import PlanAdherenceRow
from .fitness import MILE_M
from .plan import PlanDayRow, PlanWeekRow, current_version_for_week

# --- tunable constants (see adr/0001-training-plans-as-versioned-ground-truth.md)

WEEK_ON_LO = 0.90
WEEK_ON_HI = 1.10
WEEK_CLOSE_LO = 0.80
WEEK_CLOSE_HI = 1.15

# Workout count: target - actual <= 0 is "on", 1 short is "close", 2+ short is "off".
WORKOUT_ON_SHORTFALL = 0
WORKOUT_CLOSE_SHORTFALL = 1

# Any single run >= this fraction of target_long_run_miles/target_long_run_minutes counts,
# any day of the week (a week with both targets is satisfied by either).
LONG_RUN_ON_RATIO = 0.85

# Seconds/mile slack added to a frozen workout pace range before judging
# actual work-lap pace against it (see workout_pace_delta_s in the docstring above).
PACE_TOLERANCE = 10.0

_RUN_TYPES: tuple[str, ...] = ("Run", "TrailRun", "VirtualRun")

# Strength activities counted into actual_strength_days — displayed only,
# never banded or flagged (see the module docstring).
STRENGTH_SPORT_TYPES: tuple[str, ...] = ("WeightTraining", "Workout")


# --- pure data shapes --------------------------------------------------------

class WorkoutPace(TypedDict):
    date: str
    label: str | None  # classify_workout(name), if the name carries a recognizable keyword
    pace_min_per_mile: float | None  # distance-weighted work-lap pace; None if no work laps
    distance_mi: float  # work-lap distance, used to weight aggregation; 0.0 if no work laps


class PlannedWorkoutSlot(TypedDict):
    date: str
    title: str | None
    pace_lo: float | None  # frozen target range, decimal min/mi (from DayTarget)
    pace_hi: float | None
    terrain: str | None  # 'trail' excludes this slot from pace-delta contribution entirely


class WeekActuals(TypedDict):
    actual_miles: float
    actual_workouts: int
    actual_strength_days: int  # distinct dates with a synced STRENGTH_SPORT_TYPES activity
    long_run_miles: float | None  # longest single run that week (any run_type), None if no runs
    long_run_minutes: float | None  # longest single run's moving time that week, None if no runs
    workout_paces: list[WorkoutPace]


class ScoredWeek(TypedDict):
    mileage_ratio: float | None
    mileage_band: str  # on | close | off
    mileage_off_direction: str | None  # low | high | None (only set when mileage_band == "off")
    workout_band: str  # on | close | off
    long_run_done: bool | None  # None when the week has no long-run target (miles or minutes)
    workout_pace_delta_s: float | None
    band: str  # on | close | off — the worse of mileage_band and workout_band


class FlagEntry(TypedDict):
    type: str  # mileage_off_low | mileage_off_high | workout_count_short | long_run_missed
    weeks: int  # length of the consecutive run this flag summarizes
    since: str  # week_start of the run's first week
    message: str  # descriptive, numbers-first, /miles register — no scolding


class _WeekCalc(ScoredWeek):
    week_start: str
    version_n_used: int
    actual_miles: float
    actual_workouts: int
    actual_strength_days: int
    target_workouts: int
    target_miles: float | None


# --- band scoring -------------------------------------------------------------

def _mileage_band(ratio: float) -> str:
    if WEEK_ON_LO <= ratio <= WEEK_ON_HI:
        return "on"
    if WEEK_CLOSE_LO <= ratio <= WEEK_CLOSE_HI:
        return "close"
    return "off"


def _mileage_off_direction(ratio: float) -> str | None:
    if ratio < WEEK_CLOSE_LO:
        return "low"
    if ratio > WEEK_CLOSE_HI:
        return "high"
    return None


def _workout_band(actual: int, target: int) -> str:
    shortfall = target - actual
    if shortfall <= WORKOUT_ON_SHORTFALL:
        return "on"
    if shortfall == WORKOUT_CLOSE_SHORTFALL:
        return "close"
    return "off"


_BAND_RANK = {"on": 0, "close": 1, "off": 2}


def _worse_band(a: str, b: str) -> str:
    return a if _BAND_RANK[a] >= _BAND_RANK[b] else b


def _pace_delta_s(actual_pace_min_mi: float, pace_lo: float, pace_hi: float) -> float:
    """Signed seconds/mile outside [pace_lo, pace_hi] expanded by +/- PACE_TOLERANCE;
    0.0 when the actual pace falls within that tolerance-expanded range. Positive
    means slower than the range (even after slack), negative means faster."""
    tol_min = PACE_TOLERANCE / 60.0
    lo, hi = pace_lo - tol_min, pace_hi + tol_min
    if actual_pace_min_mi < lo:
        return (actual_pace_min_mi - lo) * 60.0
    if actual_pace_min_mi > hi:
        return (actual_pace_min_mi - hi) * 60.0
    return 0.0


def _match_workouts(
    slots: list[PlannedWorkoutSlot], paces: list[WorkoutPace]
) -> list[tuple[PlannedWorkoutSlot, WorkoutPace]]:
    """Matches synced workouts to planned workout slots by count. Both lists are
    date-ordered on entry. A slot whose title exactly matches (case-insensitive)
    a synced workout's classify_workout label is paired first (keyword match);
    everything left over pairs positionally in date order."""
    remaining_slots = list(slots)
    remaining_paces = list(paces)
    matched: list[tuple[PlannedWorkoutSlot, WorkoutPace]] = []

    for slot in list(remaining_slots):
        title = slot["title"]
        if not title:
            continue
        hit = next(
            (p for p in remaining_paces if p["label"] and p["label"].lower() == title.lower()),
            None,
        )
        if hit is not None:
            matched.append((slot, hit))
            remaining_slots.remove(slot)
            remaining_paces.remove(hit)

    for slot, pace in zip(remaining_slots, remaining_paces):
        matched.append((slot, pace))

    return matched


def _aggregate_pace_delta(pairs: list[tuple[PlannedWorkoutSlot, WorkoutPace]]) -> float | None:
    """Distance-weighted average of _pace_delta_s across matched pairs that have
    both a frozen target range and a computed actual pace; None if none qualify.
    A trail-terrain slot is skipped entirely — matched for count purposes, but
    grade makes road pace bands meaningless, so it never contributes here."""
    weighted_sum = 0.0
    weight_total = 0.0
    for slot, pace in pairs:
        if slot["terrain"] == "trail":
            continue
        pace_lo, pace_hi, actual = slot["pace_lo"], slot["pace_hi"], pace["pace_min_per_mile"]
        if pace_lo is None or pace_hi is None or actual is None:
            continue
        delta = _pace_delta_s(actual, pace_lo, pace_hi)
        weight = pace["distance_mi"] or 1.0
        weighted_sum += delta * weight
        weight_total += weight
    if weight_total <= 0:
        return None
    return round(weighted_sum / weight_total, 1)


def _week_mileage_score(week: PlanWeekRow, actual_miles: float) -> tuple[float | None, str, str | None]:
    """Mileage ratio/band/off_direction for one week:
      - target_miles and target_miles_hi both NULL: deliberately unspecified —
        ratio NULL, band "on" (never worsens the week's overall band; workout
        count is the only judgment for a week with no mileage target).
      - target_miles_hi set (a range week): ratio is actual vs the nearest
        bound — inside [target_miles, target_miles_hi] scores 1.0 flat, below
        target_miles scores actual/target_miles, above target_miles_hi scores
        actual/target_miles_hi.
      - point week (target_miles only): ratio is actual/target_miles, same as
        v1 — except target_miles <= 0 (e.g. a backdated week authored with no
        real target) still reads as unspecified rather than dividing by zero.
    """
    lo, hi = week["target_miles"], week["target_miles_hi"]
    if lo is None and hi is None:
        return None, "on", None

    ratio: float | None
    if hi is not None:
        assert lo is not None  # validated together at authoring — see plan.py's _validate_week_fields
        if actual_miles < lo:
            ratio = actual_miles / lo if lo > 0 else None
        elif actual_miles > hi:
            ratio = actual_miles / hi if hi > 0 else None
        else:
            ratio = 1.0
    else:
        ratio = actual_miles / lo if lo and lo > 0 else None

    if ratio is None:
        return None, "on", None
    band = _mileage_band(ratio)
    direction = _mileage_off_direction(ratio) if band == "off" else None
    return ratio, band, direction


def _long_run_target_met(
    target_miles: float | None, target_minutes: float | None,
    actual_miles: float | None, actual_minutes: float | None,
) -> bool | None:
    """None when the week has neither a mileage nor a duration long-run
    target. Otherwise True when any single run met LONG_RUN_ON_RATIO of
    either target that's set — a week carrying both is satisfied by either."""
    if (target_miles is None or target_miles <= 0) and (target_minutes is None or target_minutes <= 0):
        return None
    met_miles = (
        target_miles is not None and target_miles > 0
        and actual_miles is not None and actual_miles >= LONG_RUN_ON_RATIO * target_miles
    )
    met_minutes = (
        target_minutes is not None and target_minutes > 0
        and actual_minutes is not None and actual_minutes >= LONG_RUN_ON_RATIO * target_minutes
    )
    return bool(met_miles or met_minutes)


def score_week(
    week: PlanWeekRow, actuals: WeekActuals, workout_slots: list[PlannedWorkoutSlot]
) -> ScoredWeek:
    """Pure per-week scoring: no conn, no dates beyond what's already resolved
    into `actuals`/`workout_slots`. See module docstring for the semantics."""
    mileage_ratio, mileage_band, mileage_off_direction = _week_mileage_score(week, actuals["actual_miles"])

    workout_band = _workout_band(actuals["actual_workouts"], week["target_workouts"])

    long_run_done = _long_run_target_met(
        week["target_long_run_miles"], week["target_long_run_minutes"],
        actuals["long_run_miles"], actuals["long_run_minutes"],
    )

    pairs = _match_workouts(workout_slots, actuals["workout_paces"])
    workout_pace_delta_s = _aggregate_pace_delta(pairs)

    return {
        "mileage_ratio": mileage_ratio,
        "mileage_band": mileage_band,
        "mileage_off_direction": mileage_off_direction,
        "workout_band": workout_band,
        "long_run_done": long_run_done,
        "workout_pace_delta_s": workout_pace_delta_s,
        "band": _worse_band(mileage_band, workout_band),
    }


# --- pattern flags ------------------------------------------------------------

_SMALL_NUMS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


def _spell(n: int) -> str:
    return _SMALL_NUMS.get(n, str(n))


def _flag_runs(flagged: list[bool]) -> list[tuple[int, int]]:
    """Maximal runs of consecutive True values with length >= 2, as (start, end)
    inclusive indices. Single True values in isolation never qualify."""
    runs: list[tuple[int, int]] = []
    i, n = 0, len(flagged)
    while i < n:
        if not flagged[i]:
            i += 1
            continue
        j = i
        while j < n and flagged[j]:
            j += 1
        if j - i >= 2:
            runs.append((i, j - 1))
        i = j
    return runs


def _mileage_flag(kind: str, run: list[_WeekCalc]) -> FlagEntry:
    n = len(run)
    pcts = [f"{round(r['mileage_ratio'] * 100)}%" for r in run if r["mileage_ratio"] is not None]
    direction = "under" if kind == "mileage_off_low" else "over"
    message = f"weekly mileage ran {', '.join(pcts)} of target ({direction}) across the last {_spell(n)} weeks"
    return {"type": kind, "weeks": n, "since": run[0]["week_start"], "message": message}


def _workout_flag(run: list[_WeekCalc]) -> FlagEntry:
    n = len(run)
    pairs = {(r["actual_workouts"], r["target_workouts"]) for r in run}
    if len(pairs) == 1:
        actual, target = next(iter(pairs))
        message = f"workouts {actual} of {target} in each of the last {_spell(n)} weeks"
    else:
        parts = ", ".join(f"{r['actual_workouts']} of {r['target_workouts']}" for r in run)
        message = f"workout counts of {parts} over the last {_spell(n)} weeks"
    return {"type": "workout_count_short", "weeks": n, "since": run[0]["week_start"], "message": message}


def _long_run_flag(run: list[_WeekCalc]) -> FlagEntry:
    n = len(run)
    message = f"long run under {round(LONG_RUN_ON_RATIO * 100)}% of target in each of the last {_spell(n)} weeks"
    return {"type": "long_run_missed", "weeks": n, "since": run[0]["week_start"], "message": message}


def _build_flags(rows: list[_WeekCalc]) -> dict[str, list[FlagEntry]]:
    """One FlagEntry per maximal qualifying run (length >= 2), attached to the
    week_start of the run's last (most recent) week. rows must be ordered
    ascending by week_start (compute_plan_adherence guarantees this)."""
    flags_by_week: dict[str, list[FlagEntry]] = {}

    off_low = [r["mileage_band"] == "off" and r["mileage_off_direction"] == "low" for r in rows]
    off_high = [r["mileage_band"] == "off" and r["mileage_off_direction"] == "high" for r in rows]
    workout_short = [r["workout_band"] != "on" for r in rows]

    for start, end in _flag_runs(off_low):
        flags_by_week.setdefault(rows[end]["week_start"], []).append(
            _mileage_flag("mileage_off_low", rows[start : end + 1])
        )
    for start, end in _flag_runs(off_high):
        flags_by_week.setdefault(rows[end]["week_start"], []).append(
            _mileage_flag("mileage_off_high", rows[start : end + 1])
        )
    for start, end in _flag_runs(workout_short):
        flags_by_week.setdefault(rows[end]["week_start"], []).append(_workout_flag(rows[start : end + 1]))

    # Long run pattern is judged only over weeks that had a target at all —
    # weeks with none neither extend nor break a streak, they're simply absent.
    applicable = [r for r in rows if r["long_run_done"] is not None]
    missed = [not r["long_run_done"] for r in applicable]
    for start, end in _flag_runs(missed):
        flags_by_week.setdefault(applicable[end]["week_start"], []).append(
            _long_run_flag(applicable[start : end + 1])
        )

    return flags_by_week


# --- conn-taking orchestrator --------------------------------------------------

def _type_clause() -> tuple[str, list[str]]:
    ph = ",".join("?" * len(_RUN_TYPES))
    return f"sport_type IN ({ph})", list(_RUN_TYPES)


def _strength_type_clause() -> tuple[str, list[str]]:
    ph = ",".join("?" * len(STRENGTH_SPORT_TYPES))
    return f"sport_type IN ({ph})", list(STRENGTH_SPORT_TYPES)


def _work_lap_pace(conn: sqlite3.Connection, activity_id: int) -> tuple[float, float] | None:
    """(pace_min_per_mile, distance_mi) distance-weighted over an activity's
    work laps (lap_type='work'); None if it has none or they carry no distance/time."""
    row = conn.execute(
        "SELECT SUM(distance_m) AS d, SUM(moving_time_s) AS t FROM laps "
        "WHERE activity_id = ? AND lap_type = 'work'",
        [activity_id],
    ).fetchone()
    if row is None or not row["d"] or not row["t"]:
        return None
    distance_mi = float(row["d"]) / MILE_M
    if distance_mi <= 0:
        return None
    pace = (float(row["t"]) / 60.0) / distance_mi
    return pace, distance_mi


def _gather_actuals(conn: sqlite3.Connection, week_start: date, week_end: date) -> WeekActuals:
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(
        f"""
        SELECT activity_id, name, DATE(start_date) AS date, {effective} AS run_type,
               distance_m / {MILE_M} AS distance_mi, moving_time_s
        FROM activities
        WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        ORDER BY start_date
        """,
        tp + [week_start.isoformat(), week_end.isoformat()],
    ).fetchall()

    actual_miles = 0.0
    actual_workouts = 0
    long_run_miles: float | None = None
    long_run_minutes: float | None = None
    workout_paces: list[WorkoutPace] = []
    for r in rows:
        dist = float(r["distance_mi"] or 0.0)
        actual_miles += dist
        if long_run_miles is None or dist > long_run_miles:
            long_run_miles = dist
        minutes = float(r["moving_time_s"]) / 60.0 if r["moving_time_s"] is not None else None
        if minutes is not None and (long_run_minutes is None or minutes > long_run_minutes):
            long_run_minutes = minutes
        if r["run_type"] == "workout":
            actual_workouts += 1
            wl = _work_lap_pace(conn, int(r["activity_id"]))
            workout_paces.append({
                "date": r["date"],
                "label": classify_workout(r["name"] or ""),
                "pace_min_per_mile": wl[0] if wl is not None else None,
                "distance_mi": wl[1] if wl is not None else 0.0,
            })

    tc_s, tp_s = _strength_type_clause()
    strength_row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT DATE(start_date)) AS n
        FROM activities
        WHERE {tc_s} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        """,
        tp_s + [week_start.isoformat(), week_end.isoformat()],
    ).fetchone()
    actual_strength_days = int(strength_row["n"] or 0) if strength_row is not None else 0

    return {
        "actual_miles": round(actual_miles, 2),
        "actual_workouts": actual_workouts,
        "actual_strength_days": actual_strength_days,
        "long_run_miles": long_run_miles,
        "long_run_minutes": long_run_minutes,
        "workout_paces": workout_paces,
    }


def _planned_workout_slots(day_rows: list[PlanDayRow]) -> list[PlannedWorkoutSlot]:
    slots: list[PlannedWorkoutSlot] = []
    for d in sorted((d for d in day_rows if d["slot"] == "workout"), key=lambda d: d["date"]):
        target = json.loads(d["target_json"]) if d["target_json"] else {}
        slots.append({
            "date": d["date"],
            "title": d["title"],
            "pace_lo": target.get("pace_lo"),
            "pace_hi": target.get("pace_hi"),
            "terrain": d["terrain"],
        })
    return slots


def _target_plans(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """(plan_id, race_date) for every plan worth keeping adherence rows for:
    the one active plan (if any) plus every completed plan — so a
    plan's final adherence numbers (the retrospective's "14/16 weeks, 91% of
    planned miles") survive the athlete starting their next training cycle,
    not just the DELETE-then-rebuild of whichever plan happens to be active
    right now. Abandoned plans are excluded — there's no scoring intent for
    them."""
    rows = conn.execute(
        "SELECT plan_id, race_date FROM plans WHERE status IN ('active', 'completed')"
    ).fetchall()
    return [(int(r["plan_id"]), r["race_date"]) for r in rows]


def _sync_cutoff(conn: sqlite3.Connection) -> date:
    """The first week-Sunday NOT eligible for scoring: meta.last_sync_at's
    DATE, or today when no sync has ever stamped it (pre-v2 DBs) — see the
    unsynced-week guard in the module docstring. A week whose Sunday is
    on/after this date hasn't been fully covered by a sync yet."""
    last_sync_at = db.get_last_sync_at(conn)
    return date.fromisoformat(last_sync_at[:10]) if last_sync_at else date.today()


def compute_plan_adherence(conn: sqlite3.Connection) -> list[PlanAdherenceRow]:
    """Full recompute across every plan worth scoring (see _target_plans):
    every week fully covered by the sync (week's Sunday before the sync
    cutoff — see _sync_cutoff) scored against each week's contemporaneous
    version. Returns [] when there are no active or completed plans at all —
    the expected case for a fresh DB, and the only condition derive.py's
    _plan_adherence_pass relies on to no-op cleanly. Never raises on a
    plan-less DB."""
    cutoff = _sync_cutoff(conn)
    out: list[PlanAdherenceRow] = []
    for plan_id, race_date_str in _target_plans(conn):
        out.extend(_compute_for_plan(conn, plan_id, race_date_str, cutoff))
    return out


def _compute_for_plan(
    conn: sqlite3.Connection, plan_id: int, race_date_str: str, cutoff: date
) -> list[PlanAdherenceRow]:
    """Per-plan recompute — the body compute_plan_adherence runs once for
    each plan_id it targets."""
    v1_row = conn.execute(
        "SELECT MIN(pw.week_start) AS d FROM plan_weeks pw "
        "JOIN plan_versions pv ON pv.version_id = pw.version_id "
        "WHERE pv.plan_id = ? AND pv.version_n = 1",
        [plan_id],
    ).fetchone()
    plan_start_str: str | None = v1_row["d"] if v1_row is not None else None
    if plan_start_str is None:
        return []  # a targeted plan with no version-1 weeks yet — shouldn't happen, but never raise

    plan_start = date.fromisoformat(plan_start_str)
    race_dt = date.fromisoformat(race_date_str)
    race_monday = race_dt - timedelta(days=race_dt.weekday())

    calc_rows: list[_WeekCalc] = []
    week_start = plan_start
    while week_start <= race_monday:
        week_end = week_start + timedelta(days=6)
        if week_end >= cutoff:
            break  # this and all later weeks aren't sync-covered yet; weeks are ascending

        governing = current_version_for_week(conn, plan_id, week_start)
        if governing is None:
            week_start += timedelta(weeks=1)
            continue

        week_row = next((w for w in governing["weeks"] if w["week_start"] == week_start.isoformat()), None)
        if week_row is None:
            week_start += timedelta(weeks=1)
            continue

        day_rows = [
            d for d in governing["days"]
            if week_start.isoformat() <= d["date"] <= week_end.isoformat()
        ]
        workout_slots = _planned_workout_slots(day_rows)
        actuals = _gather_actuals(conn, week_start, week_end)
        scored = score_week(week_row, actuals, workout_slots)

        calc_rows.append({
            **scored,
            "week_start": week_start.isoformat(),
            "version_n_used": governing["version"]["version_n"],
            "actual_miles": actuals["actual_miles"],
            "actual_workouts": actuals["actual_workouts"],
            "actual_strength_days": actuals["actual_strength_days"],
            "target_workouts": week_row["target_workouts"],
            "target_miles": week_row["target_miles"],
        })
        week_start += timedelta(weeks=1)

    flags_by_week = _build_flags(calc_rows)

    out: list[PlanAdherenceRow] = []
    for r in calc_rows:
        flags = flags_by_week.get(r["week_start"])
        out.append({
            "plan_id": plan_id,
            "week_start": r["week_start"],
            "version_n_used": r["version_n_used"],
            "actual_miles": r["actual_miles"],
            "actual_workouts": r["actual_workouts"],
            "actual_strength_days": r["actual_strength_days"],
            "long_run_done": None if r["long_run_done"] is None else int(r["long_run_done"]),
            "mileage_ratio": r["mileage_ratio"],
            "workout_pace_delta_s": r["workout_pace_delta_s"],
            "band": r["band"],
            "flags_json": json.dumps(flags) if flags else None,
        })
    return out
