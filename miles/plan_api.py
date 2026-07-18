"""Plan API: read-only endpoints backing plan.html — the active plan, its
current (latest) version's weeks/days with actual mileage/workouts joined in
from synced activities, version history, and version-to-version diffs.

Plans are athlete-authored ground truth (miles/plan.py); this module only
reads and joins, never writes (the agent + MCP tools are the editor — see
adr/0001-training-plans-as-versioned-ground-truth.md). /api/plan itself only
reports plain planned-vs-actual numbers, no judgment; /api/plan-adherence
(below) layers bands/flags from the derived plan_adherence table on top.
"""

import json
import sqlite3
from datetime import date, timedelta
from collections.abc import Sequence
from typing import cast, get_args

from typing_extensions import TypedDict

from fastapi import APIRouter, HTTPException

from . import db, plan, plan_adherence
from .builds import Build, RaceRef, detect_builds
from .classifier import classify_workout
from .derive import ensure_derived
from .distance_builds import Bucket, get_distance_builds
from .fitness import MILE_M, estimate_fitness
from .format import fmt_pace, fmt_time
from .periods import WeekAgg, detect_periods
from .plan import DayTarget, PlanDayRow, PlanRow, PlanValidationError, PlanVersionRow, PlanWeekRow, VersionDiff
from .races import NOMINAL_METERS, classify_race_distance

router = APIRouter()

_RUN_TYPES = ("Run", "TrailRun", "VirtualRun")


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    ensure_derived(conn)
    return conn


def _type_clause() -> tuple[str, list[str]]:
    ph = ",".join("?" * len(_RUN_TYPES))
    return f"sport_type IN ({ph})", list(_RUN_TYPES)


class PlanActivity(TypedDict):
    activity_id: int
    name: str | None
    run_type: str
    distance_mi: float
    moving_time_s: int | None
    pace_min_per_mile: float | None


class PlanLogEntry(TypedDict):
    log_id: int
    date: str
    action: str
    reason: str | None


class PlanWeekOut(TypedDict):
    week_start: str
    target_miles: float | None  # NULL alongside target_miles_hi = deliberately unspecified week
    target_miles_hi: float | None  # range upper bound; point week = target_miles (lo) only
    target_workouts: int
    target_long_run_miles: float | None
    target_long_run_minutes: float | None
    target_strength_days: int | None
    phase: str
    note: str | None
    actual_miles: float
    actual_workouts: int
    actual_strength_days: int  # displayed only — plan_adherence never bands/flags it
    is_current: bool
    is_future: bool


class PlanDayOut(TypedDict):
    """The planned sketch for one day. Actual runs/log annotations are NOT
    nested here — see PlanResponse.actual/log — because plan_days may not
    have a row for every calendar date (planners can skip emitting rest-day
    rows), and a run or log entry should still surface on such a date."""
    date: str
    seq: int
    slot: str
    title: str | None
    target_miles: float | None
    target_minutes: float | None
    target: DayTarget | None
    terrain: str | None  # 'trail', or NULL meaning road (the default)
    note: str | None  # athlete-facing guidance, distinct from plan_log's reality-annotations


class ThisWeekStat(TypedDict):
    week_start: str
    target_miles: float | None
    target_miles_hi: float | None
    actual_miles: float
    target_workouts: int
    actual_workouts: int
    target_strength_days: int | None
    actual_strength_days: int


class GoalStat(TypedDict):
    goal_time_s: int
    equivalent_time_s: float | None
    confidence: str | None


class RemainingDayOut(TypedDict):
    date: str
    slot: str
    title: str | None
    terrain: str | None


class TodayOut(TypedDict):
    """Today's slice of the plan — always present for an active plan whose
    window covers today, regardless of sync freshness (the planned session
    itself isn't sync-derived; only the actual-so-far numbers in
    week_so_far/vs_last_week are). `sessions` is every plan_days row for
    today, seq order (a double run day, or a run plus a strength day, both
    surface); empty when today has no explicit plan_days row — an implied
    rest day (planners may skip emitting them)."""
    date: str
    sessions: list[PlanDayOut]
    is_race_day: bool
    is_race_week: bool
    week_start: str | None  # None when today falls outside the plan's week list entirely


class WeekSoFarOut(TypedDict):
    """"How's the week going?" — actual so far against the week's target, plus
    an expected-by-now marker pro-rated by planned (non-rest) days' target
    mileage elapsed over total planned mileage this week, NOT calendar
    sevenths or a plain day count (a rest-day-heavy front half, or a single
    long-run day, must not read as behind/ahead out of proportion to what was
    actually scheduled). actual_* are truncated to week_cutoff_date, the
    week-local clamp of the sync cutoff — see _week_elapsed_days."""
    week_start: str
    week_cutoff_date: str  # actual_* summed over [week_start, week_cutoff_date]
    week_started: bool  # False when the sync cutoff predates this week's Monday entirely
    actual_miles_so_far: float
    actual_workouts_so_far: int
    target_miles: float | None
    target_miles_hi: float | None
    target_workouts: int
    expected_miles_by_now: float | None  # None when the week has no mileage target, or no planned days
    remaining_planned_miles: float | None  # sum of target_miles over remaining_days; None when none of them has target_miles
    remaining_days: list[RemainingDayOut]  # non-rest days after week_cutoff_date, in date order
    phase: str
    note: str | None


class VsLastWeekDayOut(TypedDict):
    offset: int  # 0=Monday..6=Sunday
    date: str
    last_week_date: str
    miles: float
    last_week_miles: float


class VsLastWeekOut(TypedDict):
    """"vs last week at this point" — actual miles through the same weekday
    cutoff last week, both sides truncated to the sync cutoff so this is never
    stale-current vs complete-last. None (at the PlanResponse level) whenever
    the cutoff predates this week's Monday — there is nothing yet to compare."""
    week_start: str
    last_week_start: str
    cutoff_offset: int  # last elapsed weekday index (0=Mon), inclusive, both weeks
    this_week_miles: float
    last_week_miles: float
    delta_miles: float
    days: list[VsLastWeekDayOut]  # per-day (not cumulative), offsets 0..cutoff_offset


class PlanResponse(TypedDict):
    plan: PlanRow
    version: PlanVersionRow | None
    weeks: list[PlanWeekOut]
    days: list[PlanDayOut]
    actual: dict[str, list[PlanActivity]]  # keyed by date, every synced run in the plan window
    log: dict[str, list[PlanLogEntry]]  # keyed by date, every plan_log entry in the plan window
    weeks_to_race: int
    this_week: ThisWeekStat | None
    goal: GoalStat | None
    last_sync_at: str | None
    synced_through: str | None  # last_sync_at's date, only when it predates today (the freshness label trigger)
    today: TodayOut | None  # None unless the plan is active
    week_so_far: WeekSoFarOut | None  # None unless active and current week is in the plan's week list
    vs_last_week: VsLastWeekOut | None  # None unless week_so_far applies and week_started


def _actual_by_week(conn: sqlite3.Connection, start: str, end: str) -> dict[str, tuple[float, int]]:
    """Monday-aligned actual miles/workout counts per week over [start, end]
    (inclusive), same convention as api.py's _week_aggs / EFFECTIVE_RUN_TYPE_SQL."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            SUM(CASE WHEN {effective} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        GROUP BY monday
    """, tp + [start, end]).fetchall()
    return {r["monday"]: (r["miles"] or 0.0, r["workouts"] or 0) for r in rows}


def _actual_totals(conn: sqlite3.Connection, start: str, end: str) -> tuple[float, int, int]:
    """Actual miles/workout count/run count summed over [start, end] inclusive
    — a plain range aggregate (unlike _actual_by_week, not grouped by
    Monday), for a partial-week window such as "this week so far"."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    row = conn.execute(f"""
        SELECT ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
               SUM(CASE WHEN {effective} = 'workout' THEN 1 ELSE 0 END) AS workouts,
               COUNT(*) AS runs
        FROM activities
        WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
    """, tp + [start, end]).fetchone()
    return (row["miles"] or 0.0, row["workouts"] or 0, row["runs"] or 0)


def _daily_miles(conn: sqlite3.Connection, start: str, end: str) -> dict[str, float]:
    """Per-date actual miles over [start, end] inclusive, keyed by ISO date —
    the day-level series behind the vs-last-week comparison."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, ROUND(SUM(distance_m) / 1609.34, 2) AS miles
        FROM activities
        WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        GROUP BY DATE(start_date)
    """, tp + [start, end]).fetchall()
    return {r["date"]: (r["miles"] or 0.0) for r in rows}


def _strength_days_by_week(conn: sqlite3.Connection, start: str, end: str) -> dict[str, int]:
    """Monday-aligned count of distinct dates with a synced
    plan_adherence.STRENGTH_SPORT_TYPES activity — same shape/convention as
    _actual_by_week, kept separate since strength activities fall outside
    _type_clause's run-only sport types."""
    ph = ",".join("?" * len(plan_adherence.STRENGTH_SPORT_TYPES))
    rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            COUNT(DISTINCT DATE(start_date)) AS strength_days
        FROM activities
        WHERE sport_type IN ({ph}) AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        GROUP BY monday
    """, list(plan_adherence.STRENGTH_SPORT_TYPES) + [start, end]).fetchall()
    return {r["monday"]: int(r["strength_days"] or 0) for r in rows}


def _activities_by_date(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[PlanActivity]]:
    """Every synced run in [start, end] (inclusive), keyed by calendar date —
    the calendar's "actual run(s) filled over the planned slot" layer."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT
            activity_id, name, DATE(start_date) AS date, {effective} AS run_type,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile
        FROM activities
        WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        ORDER BY start_date
    """, tp + [start, end]).fetchall()
    out: dict[str, list[PlanActivity]] = {}
    for r in rows:
        out.setdefault(r["date"], []).append(PlanActivity(
            activity_id=int(r["activity_id"]),
            name=r["name"],
            run_type=r["run_type"],
            distance_mi=r["distance_mi"] or 0.0,
            moving_time_s=r["moving_time_s"],
            pace_min_per_mile=r["pace_min_per_mile"],
        ))
    return out


def _log_by_date(conn: sqlite3.Connection, plan_id: int, start: str, end: str) -> dict[str, list[PlanLogEntry]]:
    """plan_log entries in [start, end] (inclusive), keyed by date — the
    calendar's small annotation markers ("skipped Tue, slept badly")."""
    rows = conn.execute(
        "SELECT log_id, date, action, reason FROM plan_log "
        "WHERE plan_id = ? AND date >= ? AND date <= ? ORDER BY date, log_id",
        [plan_id, start, end],
    ).fetchall()
    out: dict[str, list[PlanLogEntry]] = {}
    for r in rows:
        out.setdefault(r["date"], []).append(PlanLogEntry(
            log_id=int(r["log_id"]), date=r["date"], action=r["action"], reason=r["reason"],
        ))
    return out


def _week_elapsed_days(monday: date, week_cutoff: date) -> int | None:
    """Days elapsed into the week [0=Monday..6=Sunday] as of week_cutoff,
    clamped to that range, or None when week_cutoff predates monday entirely
    (the sync cutoff hasn't reached this week yet — nothing has "happened"
    in it as far as the data can say, so callers must not report a partial
    week at all rather than defaulting to zero elapsed days)."""
    raw = (week_cutoff - monday).days
    if raw < 0:
        return None
    return min(raw, 6)


class ActualWeekTotals(TypedDict):
    """The plan-independent half of "how's this week going" — actual-only
    totals for the calendar week starting at monday, truncated to the global
    sync cutoff. Shared by /api/today (which reports this as-is) and
    _today_blocks (which layers plan targets/expected-by-now on top)."""
    week_start: str
    week_cutoff_date: str
    week_started: bool
    actual_miles: float
    actual_run_count: int
    actual_workout_count: int
    actual_strength_days: int


def _actual_week_totals(conn: sqlite3.Connection, monday: date, cutoff_date: date) -> ActualWeekTotals:
    elapsed = _week_elapsed_days(monday, cutoff_date)
    week_started = elapsed is not None
    week_cutoff_date = (monday + timedelta(days=elapsed)) if elapsed is not None else monday - timedelta(days=1)
    if week_started:
        miles, workouts, runs = _actual_totals(conn, monday.isoformat(), week_cutoff_date.isoformat())
        strength = _strength_days_by_week(conn, monday.isoformat(), week_cutoff_date.isoformat()).get(monday.isoformat(), 0)
    else:
        miles, workouts, runs, strength = 0.0, 0, 0, 0
    return ActualWeekTotals(
        week_start=monday.isoformat(),
        week_cutoff_date=week_cutoff_date.isoformat(),
        week_started=week_started,
        actual_miles=miles,
        actual_run_count=runs,
        actual_workout_count=workouts,
        actual_strength_days=strength,
    )


def _vs_last_week_block(conn: sqlite3.Connection, monday: date, elapsed: int) -> VsLastWeekOut:
    """"vs last week at this point" over the first `elapsed` days of monday's
    week vs the same weekday span the week before — the plan-independent
    computation _today_blocks also uses once it knows the week has started."""
    monday_iso = monday.isoformat()
    week_cutoff_date = monday + timedelta(days=elapsed)
    last_week_monday = monday - timedelta(days=7)
    last_week_cutoff = last_week_monday + timedelta(days=elapsed)
    daily_this = _daily_miles(conn, monday_iso, week_cutoff_date.isoformat())
    daily_last = _daily_miles(conn, last_week_monday.isoformat(), last_week_cutoff.isoformat())
    days_out: list[VsLastWeekDayOut] = []
    for offset in range(elapsed + 1):
        d_this = (monday + timedelta(days=offset)).isoformat()
        d_last = (last_week_monday + timedelta(days=offset)).isoformat()
        days_out.append(VsLastWeekDayOut(
            offset=offset, date=d_this, last_week_date=d_last,
            miles=daily_this.get(d_this, 0.0), last_week_miles=daily_last.get(d_last, 0.0),
        ))
    this_week_miles = round(sum(x["miles"] for x in days_out), 2)
    last_week_miles = round(sum(x["last_week_miles"] for x in days_out), 2)
    return VsLastWeekOut(
        week_start=monday_iso,
        last_week_start=last_week_monday.isoformat(),
        cutoff_offset=elapsed,
        this_week_miles=this_week_miles,
        last_week_miles=last_week_miles,
        delta_miles=round(this_week_miles - last_week_miles, 2),
        days=days_out,
    )


def _sync_cutoff_date(conn: sqlite3.Connection, today: date) -> date:
    """min(today, last sync's date) — the global sync cutoff every
    actual-so-far number in this module truncates to, so a stale sync never
    reads as a day's worth of zero mileage. Falls back to today when there's
    never been a sync."""
    last_sync_at = db.get_last_sync_at(conn)
    sync_date = date.fromisoformat(last_sync_at[:10]) if last_sync_at else today
    return min(today, sync_date)


def _today_blocks(
    conn: sqlite3.Connection,
    active: PlanRow,
    weeks_in: list[PlanWeekRow],
    days_in: list[PlanDayRow],
    today: date,
    cutoff_date: date,
) -> tuple[TodayOut | None, WeekSoFarOut | None, VsLastWeekOut | None]:
    """Today/week_so_far/vs_last_week — computed only for an active plan; see
    PlanResponse field docstrings for the semantics of each. cutoff_date is
    the global sync cutoff (min(today, last_sync_at's date), or today when
    there's never been a sync)."""
    if active["status"] != "active":
        return None, None, None

    monday = today - timedelta(days=today.weekday())
    monday_iso = monday.isoformat()
    week = next((w for w in weeks_in if w["week_start"] == monday_iso), None)

    race_dt = date.fromisoformat(active["race_date"])
    today_out = TodayOut(
        date=today.isoformat(),
        sessions=[_day_out(d) for d in days_in if d["date"] == today.isoformat()],
        is_race_day=race_dt == today,
        is_race_week=monday <= race_dt <= monday + timedelta(days=6),
        week_start=monday_iso if week is not None else None,
    )
    if week is None:
        return today_out, None, None

    week_days = [d for d in days_in if monday_iso <= d["date"] <= (monday + timedelta(days=6)).isoformat()]
    totals = _actual_week_totals(conn, monday, cutoff_date)
    week_started = totals["week_started"]
    week_cutoff_date = date.fromisoformat(totals["week_cutoff_date"])
    actual_miles_so_far, actual_workouts_so_far = totals["actual_miles"], totals["actual_workout_count"]
    elapsed = _week_elapsed_days(monday, cutoff_date)

    planned_days = [d for d in week_days if d["slot"] != "rest"]
    elapsed_planned = [d for d in planned_days if d["date"] <= week_cutoff_date.isoformat()]
    remaining_planned = [d for d in planned_days if d["date"] > week_cutoff_date.isoformat()]

    # Mileage-weighted pro-rate: a day's share of the week's expectation is its
    # own target_miles (strength/duration-only days with target_miles = None
    # weigh 0), not a flat 1/len(planned_days) — moving mileage between days,
    # or a single heavy long-run day, must not distort "expected by now".
    # Falls back to the day-count ratio only when no planned day carries a
    # mileage target at all (the weighted ratio would be 0/0).
    total_planned_miles = sum(d["target_miles"] or 0.0 for d in planned_days)
    elapsed_planned_miles = sum(d["target_miles"] or 0.0 for d in elapsed_planned)
    if total_planned_miles > 0:
        planned_fraction = elapsed_planned_miles / total_planned_miles
    elif planned_days:
        planned_fraction = len(elapsed_planned) / len(planned_days)
    else:
        planned_fraction = None
    basis_target = (
        (week["target_miles"] + week["target_miles_hi"]) / 2.0
        if week["target_miles"] is not None and week["target_miles_hi"] is not None
        else week["target_miles"]
    )
    expected_miles_by_now = (
        round(basis_target * planned_fraction, 1)
        if basis_target is not None and planned_fraction is not None and week_started
        else None
    )
    remaining_days = [
        RemainingDayOut(date=d["date"], slot=d["slot"], title=d["title"], terrain=d["terrain"])
        for d in remaining_planned
    ]
    remaining_planned_miles = (
        round(sum(d["target_miles"] or 0.0 for d in remaining_planned), 1)
        if any(d["target_miles"] is not None for d in remaining_planned)
        else None
    )

    week_so_far = WeekSoFarOut(
        week_start=monday_iso,
        week_cutoff_date=week_cutoff_date.isoformat(),
        week_started=week_started,
        actual_miles_so_far=actual_miles_so_far,
        actual_workouts_so_far=actual_workouts_so_far,
        target_miles=week["target_miles"],
        target_miles_hi=week["target_miles_hi"],
        target_workouts=week["target_workouts"],
        expected_miles_by_now=expected_miles_by_now,
        remaining_planned_miles=remaining_planned_miles,
        remaining_days=remaining_days,
        phase=week["phase"],
        note=week["note"],
    )

    vs_last_week = _vs_last_week_block(conn, monday, elapsed) if week_started and elapsed is not None else None

    return today_out, week_so_far, vs_last_week


def _day_out(d: PlanDayRow) -> PlanDayOut:
    target = cast(DayTarget, json.loads(d["target_json"])) if d["target_json"] else None
    return PlanDayOut(
        date=d["date"], seq=d["seq"], slot=d["slot"], title=d["title"],
        target_miles=d["target_miles"], target_minutes=d["target_minutes"], target=target,
        terrain=d["terrain"], note=d["note"],
    )


@router.get("/api/plan")
def get_plan() -> PlanResponse | None:
    """
    The active plan's latest version — weeks and days — with actual weekly
    miles/workout counts and per-day synced runs/log annotations joined in
    from activities/plan_log. Returns null when there's no active plan (the
    empty state plan.html renders, pointing at the /miles-plan skill).

    "Current version" here always means the latest authored version (highest
    version_n), not the per-week contemporaneous version the adherence
    scoring uses — this endpoint is "what does the plan say now", not "what
    was a past week scored against".

    When there's no active plan, falls back to the most recently raced
    completed plan (see plan.get_current_or_recent_plan) so a
    just-finished plan's retrospective (calendar, version history, and
    /api/plan-retrospective) stays reachable instead of going empty the
    moment auto-complete flips its status. Returns null only when there is
    neither an active nor a completed plan.
    """
    conn = _conn()
    active = plan.get_current_or_recent_plan(conn)
    if active is None:
        return None

    latest = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? ORDER BY version_n DESC LIMIT 1",
        [active["plan_id"]],
    ).fetchone()
    bundle = plan.get_version(conn, int(latest["version_id"])) if latest is not None else None

    weeks_in = bundle["weeks"] if bundle is not None else []
    days_in = bundle["days"] if bundle is not None else []

    today = date.today()
    current_monday = (today - timedelta(days=today.weekday())).isoformat()

    weeks_out: list[PlanWeekOut] = []
    this_week: ThisWeekStat | None = None
    if weeks_in:
        actual_weeks = _actual_by_week(conn, weeks_in[0]["week_start"], weeks_in[-1]["week_start"])
        strength_weeks = _strength_days_by_week(conn, weeks_in[0]["week_start"], weeks_in[-1]["week_start"])
        for w in weeks_in:
            actual_miles, actual_workouts = actual_weeks.get(w["week_start"], (0.0, 0))
            actual_strength_days = strength_weeks.get(w["week_start"], 0)
            weeks_out.append(PlanWeekOut(
                week_start=w["week_start"],
                target_miles=w["target_miles"],
                target_miles_hi=w["target_miles_hi"],
                target_workouts=w["target_workouts"],
                target_long_run_miles=w["target_long_run_miles"],
                target_long_run_minutes=w["target_long_run_minutes"],
                target_strength_days=w["target_strength_days"],
                phase=w["phase"],
                note=w["note"],
                actual_miles=actual_miles,
                actual_workouts=actual_workouts,
                actual_strength_days=actual_strength_days,
                is_current=w["week_start"] == current_monday,
                is_future=w["week_start"] > current_monday,
            ))
            if w["week_start"] == current_monday:
                this_week = ThisWeekStat(
                    week_start=w["week_start"],
                    target_miles=w["target_miles"],
                    target_miles_hi=w["target_miles_hi"],
                    actual_miles=actual_miles,
                    target_workouts=w["target_workouts"],
                    actual_workouts=actual_workouts,
                    target_strength_days=w["target_strength_days"],
                    actual_strength_days=actual_strength_days,
                )

    days_out: list[PlanDayOut] = [_day_out(d) for d in days_in]
    activities: dict[str, list[PlanActivity]] = {}
    logs: dict[str, list[PlanLogEntry]] = {}
    if weeks_in:
        window_start = weeks_in[0]["week_start"]
        window_end = (date.fromisoformat(weeks_in[-1]["week_start"]) + timedelta(days=6)).isoformat()
        activities = _activities_by_date(conn, window_start, window_end)
        logs = _log_by_date(conn, active["plan_id"], window_start, window_end)

    race_dt = date.fromisoformat(active["race_date"])
    race_monday = race_dt - timedelta(days=race_dt.weekday())
    current_monday_dt = date.fromisoformat(current_monday)
    weeks_to_race = max(0, (race_monday - current_monday_dt).days // 7)

    goal: GoalStat | None = None
    if active["goal_time_s"] is not None:
        equivalent_time_s: float | None = None
        confidence: str | None = None
        est = estimate_fitness(conn, today)
        if est is not None and active["distance_bucket"] in NOMINAL_METERS:
            pace = est["predicted"].get(active["distance_bucket"])
            if pace is not None:
                nominal_m = NOMINAL_METERS[active["distance_bucket"]]
                equivalent_time_s = round(pace * (nominal_m / MILE_M) * 60.0, 1)
                confidence = est["confidence"]
        goal = GoalStat(goal_time_s=active["goal_time_s"], equivalent_time_s=equivalent_time_s, confidence=confidence)

    last_sync_at = db.get_last_sync_at(conn)
    cutoff_date = _sync_cutoff_date(conn, today)
    synced_through = cutoff_date.isoformat() if cutoff_date < today else None
    today_out, week_so_far, vs_last_week = _today_blocks(conn, active, weeks_in, days_in, today, cutoff_date)

    return PlanResponse(
        plan=active,
        version=bundle["version"] if bundle is not None else None,
        weeks=weeks_out,
        days=days_out,
        actual=activities,
        log=logs,
        weeks_to_race=weeks_to_race,
        this_week=this_week,
        goal=goal,
        last_sync_at=last_sync_at,
        synced_through=synced_through,
        today=today_out,
        week_so_far=week_so_far,
        vs_last_week=vs_last_week,
    )


class TodayResponse(TypedDict):
    """Actual-only "how's today/this week going", independent of whether any
    plan exists at all — plan.html's Today tab falls back to this (instead of
    /api/plan's plan-layered today/week_so_far/vs_last_week) whenever there's
    no active plan to show targets against. Shares its week-so-far/
    vs-last-week math with _today_blocks via _actual_week_totals/
    _vs_last_week_block, so the two never compute "actual miles so far"
    differently."""
    date: str
    week_start: str
    week_cutoff_date: str
    runs_today: list[PlanActivity]
    week_so_far: ActualWeekTotals
    vs_last_week: VsLastWeekOut | None
    actual: dict[str, list[PlanActivity]]  # keyed by date, this week + last week — feeds the day popover


@router.get("/api/today")
def get_today() -> TodayResponse:
    """
    Plan-independent twin of /api/plan's today/week_so_far/vs_last_week
    trio — works regardless of plan state (no plan, completed-only plan, or
    even an active one, though plan.html prefers /api/plan's richer version
    whenever a plan is active). `actual` spans this week plus last week
    (Monday of last week through Sunday of this week) so the day popover has
    everything it needs without a second request.
    """
    conn = _conn()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    cutoff_date = _sync_cutoff_date(conn, today)

    totals = _actual_week_totals(conn, monday, cutoff_date)
    elapsed = _week_elapsed_days(monday, cutoff_date)
    vs_last_week = _vs_last_week_block(conn, monday, elapsed) if elapsed is not None else None

    last_week_monday = monday - timedelta(days=7)
    week_end = monday + timedelta(days=6)
    actual = _activities_by_date(conn, last_week_monday.isoformat(), week_end.isoformat())

    return TodayResponse(
        date=today.isoformat(),
        week_start=monday.isoformat(),
        week_cutoff_date=totals["week_cutoff_date"],
        runs_today=actual.get(today.isoformat(), []),
        week_so_far=totals,
        vs_last_week=vs_last_week,
        actual=actual,
    )


@router.get("/api/plan-versions")
def get_plan_versions() -> list[PlanVersionRow]:
    """Every version of the active plan (or the most recently completed one —
    see plan.get_current_or_recent_plan), oldest first — the source for
    plan.html's version history list. Empty list when there's neither."""
    conn = _conn()
    active = plan.get_current_or_recent_plan(conn)
    if active is None:
        return []
    rows = conn.execute(
        "SELECT version_id, plan_id, version_n, created_at, note, author "
        "FROM plan_versions WHERE plan_id = ? ORDER BY version_n",
        [active["plan_id"]],
    ).fetchall()
    return [
        PlanVersionRow(
            version_id=int(r["version_id"]), plan_id=int(r["plan_id"]), version_n=int(r["version_n"]),
            created_at=r["created_at"], note=r["note"], author=r["author"],
        )
        for r in rows
    ]


@router.get("/api/plan-diff")
def get_plan_diff(a: int, b: int) -> VersionDiff:
    """Thin wrapper over plan.py's diff_versions: changed weeks (which target
    fields changed) and added/removed/changed days between version ids a and
    b. 404s when either version id doesn't exist."""
    conn = _conn()
    try:
        return plan.diff_versions(conn, a, b)
    except PlanValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))


class WeekAdherenceOut(TypedDict):
    week_start: str
    version_n_used: int | None
    target_miles: float | None
    target_miles_hi: float | None
    actual_miles: float | None
    mileage_ratio: float | None
    target_workouts: int | None
    actual_workouts: int | None
    target_long_run_miles: float | None
    target_long_run_minutes: float | None
    long_run_done: bool | None
    target_strength_days: int | None
    actual_strength_days: int | None
    workout_pace_delta_s: float | None
    band: str | None


class FlagOut(TypedDict):
    type: str
    weeks: int
    since: str
    message: str


class PlanAdherenceResponse(TypedDict):
    weeks: list[WeekAdherenceOut]
    flags: list[FlagOut]  # currently active patterns only — attached to the latest completed week
    completed: int
    on_or_close: int
    headline: str  # e.g. "12 of 13 weeks on plan"


def _adherence_rows(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    """Every plan_adherence row for one plan, joined back to that same
    week's contemporaneous targets (mileage range, long-run miles/minutes,
    strength days) — shared by get_plan_adherence and the retrospective's
    final-adherence rollup (get_plan_retrospective) so both read off one
    query."""
    return conn.execute("""
        SELECT
            pa.week_start, pa.version_n_used, pa.actual_miles, pa.actual_workouts,
            pa.actual_strength_days, pa.long_run_done, pa.mileage_ratio,
            pa.workout_pace_delta_s, pa.band, pa.flags_json,
            pw.target_miles, pw.target_miles_hi, pw.target_workouts,
            pw.target_long_run_miles, pw.target_long_run_minutes, pw.target_strength_days
        FROM plan_adherence pa
        JOIN plan_versions pv ON pv.plan_id = pa.plan_id AND pv.version_n = pa.version_n_used
        JOIN plan_weeks pw ON pw.version_id = pv.version_id AND pw.week_start = pa.week_start
        WHERE pa.plan_id = ?
        ORDER BY pa.week_start
    """, [plan_id]).fetchall()


@router.get("/api/plan-adherence")
def get_plan_adherence() -> PlanAdherenceResponse | None:
    """
    The judgment layer over plan_adherence (derived, rebuilt by derive_all —
    see miles/plan_adherence.py): one row per completed week of the active
    plan (week's Sunday before today), each scored against the plan version
    that governed it at the time (version_n_used), joined back to that same
    version's week targets so the ratio/target numbers displayed always match
    what was actually scored.

    `flags` surfaces only the patterns still active as of the most recently
    completed week (plan_adherence.flags_json on earlier weeks records
    patterns that were active as of *that* week, including ones since
    resolved — this endpoint deliberately narrows to "still true now" so an
    old, resolved pattern doesn't linger in the UI). Empty when there's no
    active pattern — the common case by design.

    `on_or_close` counts weeks banded "on" or "close" — a close-mileage week
    that hit its workouts is a normal week of marathon training, not a miss.

    Returns null when there's neither an active nor a completed plan (see
    plan.get_current_or_recent_plan), or the plan hasn't reached its first
    completed week yet.
    """
    conn = _conn()
    active = plan.get_current_or_recent_plan(conn)
    if active is None:
        return None

    rows = _adherence_rows(conn, active["plan_id"])
    if not rows:
        return None

    weeks_out = [
        WeekAdherenceOut(
            week_start=r["week_start"],
            version_n_used=r["version_n_used"],
            target_miles=r["target_miles"],
            target_miles_hi=r["target_miles_hi"],
            actual_miles=r["actual_miles"],
            mileage_ratio=r["mileage_ratio"],
            target_workouts=r["target_workouts"],
            actual_workouts=r["actual_workouts"],
            target_long_run_miles=r["target_long_run_miles"],
            target_long_run_minutes=r["target_long_run_minutes"],
            long_run_done=bool(r["long_run_done"]) if r["long_run_done"] is not None else None,
            target_strength_days=r["target_strength_days"],
            actual_strength_days=r["actual_strength_days"],
            workout_pace_delta_s=r["workout_pace_delta_s"],
            band=r["band"],
        )
        for r in rows
    ]
    on_or_close = sum(1 for r in rows if r["band"] in ("on", "close"))
    completed = len(rows)
    last_flags_json = rows[-1]["flags_json"]
    flags: list[FlagOut] = json.loads(last_flags_json) if last_flags_json else []

    return PlanAdherenceResponse(
        weeks=weeks_out,
        flags=flags,
        completed=completed,
        on_or_close=on_or_close,
        headline=f"{on_or_close} of {completed} weeks on plan",
    )


# --- progression stats -------------------------------------------------------
#
# Weekly mileage vs ramp (chart 1) already exists on plan.html from /api/plan +
# /api/plan-adherence (bands); flag shading is a pure plan.html change
# reading adherence.flags — deliberately not duplicated here. This endpoint
# covers the other two: easy HR/pace progression and workout pace progression.


class WeekEasyOut(TypedDict):
    week_start: str
    avg_pace_min_per_mile: float | None
    avg_hr: float | None
    avg_apparent_temp_f: float | None
    run_count: int


class WorkoutPoint(TypedDict):
    date: str
    week_start: str
    title: str | None
    label: str | None
    pace_min_per_mile: float | None  # None when the workout has no qualifying work laps
    distance_mi: float
    pace_lo: float | None  # frozen target band from the version governing this workout's week
    pace_hi: float | None
    zone_name: str | None


class CheckpointPoint(TypedDict):
    month: str
    pace_5k: float | None


class ProgressionCaptions(TypedDict):
    easy: str  # "" when there isn't enough data to say anything
    workouts: str


class PlanProgressionResponse(TypedDict):
    weeks: list[str]  # continuous plan-window Mondays (latest version's weeks) — the shared x-axis
    easy: list[WeekEasyOut]  # aligned 1:1 with `weeks`
    workouts: list[WorkoutPoint]  # sparse — one entry per matched workout, chronological
    checkpoints: list[CheckpointPoint]  # fitness_checkpoints months landing inside the plan window
    captions: ProgressionCaptions


def _easy_progression(conn: sqlite3.Connection, week_starts: list[str]) -> list[WeekEasyOut]:
    """Per-week distance-weighted easy-run avg pace/HR, aligned 1:1 with
    week_starts (continuous plan-window Mondays) so a week with no easy runs
    synced comes back all-None — a gap in the chart, never a missing category.
    Filters to the effective run type 'easy' (COALESCE'd inferred type, same
    idiom as the rest of this module — get_easy_hr_trend's mcp tool filters
    the raw column instead, but a long_run week must never count here even
    when Strava-untagged, so the effective type is the correct filter).
    Distance-weighted rather than get_easy_hr_trend's flat per-run average —
    a single 2-mile shakeout shouldn't move the week's number as much as an
    8-mile aerobic day. avg_apparent_temp_f is an unweighted mean of the
    week's easy-run weather rows — a context annotation, not a headline
    number, so a plain average is enough."""
    if not week_starts:
        return []
    effective = db.effective_run_type_sql("a")
    ph = ",".join("?" * len(_RUN_TYPES))
    start = week_starts[0]
    end = (date.fromisoformat(week_starts[-1]) + timedelta(days=6)).isoformat()
    rows = conn.execute(f"""
        SELECT
            DATE(a.start_date, '-' || ((CAST(strftime('%w', a.start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            SUM(a.distance_m) AS total_distance_m,
            SUM(a.moving_time_s) AS total_time_s,
            SUM(CASE WHEN a.average_heartrate IS NOT NULL THEN a.distance_m * a.average_heartrate ELSE 0 END) AS hr_weighted,
            SUM(CASE WHEN a.average_heartrate IS NOT NULL THEN a.distance_m ELSE 0 END) AS hr_weight,
            AVG(w.apparent_temp_c_max) AS avg_apparent_temp_c,
            COUNT(*) AS run_count
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.sport_type IN ({ph}) AND {effective} = 'easy'
          AND DATE(a.start_date) >= ? AND DATE(a.start_date) <= ?
        GROUP BY monday
    """, list(_RUN_TYPES) + [start, end]).fetchall()
    by_week = {r["monday"]: r for r in rows}

    out: list[WeekEasyOut] = []
    for ws in week_starts:
        r = by_week.get(ws)
        if r is None or not r["total_distance_m"]:
            out.append(WeekEasyOut(
                week_start=ws, avg_pace_min_per_mile=None, avg_hr=None,
                avg_apparent_temp_f=None, run_count=0,
            ))
            continue
        distance_mi = float(r["total_distance_m"]) / MILE_M
        pace = (float(r["total_time_s"]) / 60.0) / distance_mi if r["total_time_s"] and distance_mi > 0 else None
        hr = round(float(r["hr_weighted"]) / float(r["hr_weight"]), 1) if r["hr_weight"] else None
        temp_c = r["avg_apparent_temp_c"]
        temp_f = round(float(temp_c) * 9.0 / 5 + 32) if temp_c is not None else None
        out.append(WeekEasyOut(
            week_start=ws,
            avg_pace_min_per_mile=round(pace, 2) if pace is not None else None,
            avg_hr=hr,
            avg_apparent_temp_f=temp_f,
            run_count=int(r["run_count"]),
        ))
    return out


def _workout_points(conn: sqlite3.Connection, plan_id: int, weeks_in: list[PlanWeekRow]) -> list[WorkoutPoint]:
    """One point per synced workout matched to its planned slot, reusing
    plan_adherence's count/keyword matching as-is, so this chart and the
    adherence score never disagree about which run was the workout. Each point
    carries the frozen target band from whichever version governed its week
    at the time — not necessarily the latest version, so a since-revised
    target is never retroactively attributed to a workout run under the old
    one. A week with no synced workouts contributes nothing; a matched
    workout with no qualifying work laps contributes a point with
    pace_min_per_mile=None (the frontend renders it as a gap, not a dot)."""
    today = date.today()
    points: list[WorkoutPoint] = []
    for w in weeks_in:
        week_start = date.fromisoformat(w["week_start"])
        if week_start > today:
            continue  # nothing synced yet for a future week
        week_end = week_start + timedelta(days=6)
        governing = plan.current_version_for_week(conn, plan_id, week_start)
        if governing is None:
            continue
        day_rows = [d for d in governing["days"] if w["week_start"] <= d["date"] <= week_end.isoformat()]
        slots = plan_adherence._planned_workout_slots(day_rows)
        actuals = plan_adherence._gather_actuals(conn, week_start, week_end)
        targets_by_date = {d["date"]: json.loads(d["target_json"]) for d in day_rows if d["target_json"]}
        for slot, pace in plan_adherence._match_workouts(slots, actuals["workout_paces"]):
            target = targets_by_date.get(slot["date"])
            points.append(WorkoutPoint(
                date=pace["date"],
                week_start=w["week_start"],
                title=slot["title"],
                label=pace["label"],
                pace_min_per_mile=pace["pace_min_per_mile"],
                distance_mi=pace["distance_mi"],
                pace_lo=slot["pace_lo"],
                pace_hi=slot["pace_hi"],
                zone_name=target.get("zone_name") if target else None,
            ))
    points.sort(key=lambda p: p["date"])
    return points


def _checkpoints_in_window(conn: sqlite3.Connection, week_starts: list[str]) -> list[CheckpointPoint]:
    """fitness_checkpoints months landing inside [week_starts[0], week_starts[-1]]
    — background context for the workout-pace chart, per training.html's
    monthly-checkpoint treatment. Takes a plain Monday list rather than
    PlanWeekRows so it works for both the plan-scoped and plan-independent
    (rolling-window) progression endpoints."""
    if not week_starts:
        return []
    start_month = week_starts[0][:7]
    end_month = week_starts[-1][:7]
    rows = conn.execute(
        "SELECT month, pace_5k FROM fitness_checkpoints WHERE month >= ? AND month <= ? ORDER BY month",
        [start_month, end_month],
    ).fetchall()
    return [CheckpointPoint(month=r["month"], pace_5k=r["pace_5k"]) for r in rows]


def _easy_caption(weeks: list[WeekEasyOut]) -> str:
    """"easy pace 8:41->8:25/mi over 6 weeks; avg HR 142->138" — first vs last
    data-bearing week for each metric independently (a week can have pace but
    no HR, or vice versa); "" when fewer than 2 weeks have either metric."""
    pace_pts = [(w["week_start"], w["avg_pace_min_per_mile"]) for w in weeks if w["avg_pace_min_per_mile"] is not None]
    hr_pts = [w["avg_hr"] for w in weeks if w["avg_hr"] is not None]

    parts: list[str] = []
    if len(pace_pts) >= 2:
        (first_ws, first_pace), (last_ws, last_pace) = pace_pts[0], pace_pts[-1]
        n_weeks = (date.fromisoformat(last_ws) - date.fromisoformat(first_ws)).days // 7 + 1
        parts.append(f"easy pace {fmt_pace(first_pace)}→{fmt_pace(last_pace)}/mi over {n_weeks} weeks")
    if len(hr_pts) >= 2:
        parts.append(f"avg HR {hr_pts[0]:g}→{hr_pts[-1]:g}")
    return "; ".join(parts)


def _workout_caption(points: Sequence[WorkoutPoint]) -> str:
    """"5 of 7 workouts within target pace band; latest 6:52/mi vs 6:40-6:58"
    — "" when no workout has both a computed pace and a frozen band to judge
    it against."""
    qualifying: list[tuple[float, float, float]] = [
        (p["pace_min_per_mile"], p["pace_lo"], p["pace_hi"])
        for p in points
        if p["pace_min_per_mile"] is not None and p["pace_lo"] is not None and p["pace_hi"] is not None
    ]
    if not qualifying:
        return ""
    in_band = sum(1 for pace, lo, hi in qualifying if lo <= pace <= hi)
    latest_pace, latest_lo, latest_hi = qualifying[-1]
    return (
        f"{in_band} of {len(qualifying)} workouts within target pace band; "
        f"latest {fmt_pace(latest_pace)}/mi vs "
        f"{fmt_pace(latest_lo)}–{fmt_pace(latest_hi)}"
    )


# Weeks of pre-plan history prepended to the Trends charts' x-axis.
_PLAN_PROGRESSION_LOOKBACK_WEEKS = 4


@router.get("/api/plan-progression")
def get_plan_progression() -> PlanProgressionResponse | None:
    """
    The "is it working" data, scoped to the active plan's window — the other
    two of the three progression charts (weekly mileage vs ramp already exists
    from /api/plan + /api/plan-adherence's bands; see the module comment
    above).

    `weeks` is the plan-window Monday list, prepended with
    _PLAN_PROGRESSION_LOOKBACK_WEEKS Mondays before plan start.

    `easy`: one row per week in `weeks`; see _easy_progression.

    `workouts`: one point per synced workout, chronological. Lookback points
    carry no target band; in-plan points do, via _workout_points.

    `checkpoints`: monthly 5K-pace fitness_checkpoints rows in the window.

    Returns null when there's neither an active nor a completed plan (see
    plan.get_current_or_recent_plan).
    """
    conn = _conn()
    active = plan.get_current_or_recent_plan(conn)
    if active is None:
        return None

    latest = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? ORDER BY version_n DESC LIMIT 1",
        [active["plan_id"]],
    ).fetchone()
    bundle = plan.get_version(conn, int(latest["version_id"])) if latest is not None else None
    weeks_in = bundle["weeks"] if bundle is not None else []
    plan_week_starts = [w["week_start"] for w in weeks_in]

    if plan_week_starts:
        first_monday = date.fromisoformat(plan_week_starts[0])
        lookback_starts = [
            (first_monday - timedelta(weeks=_PLAN_PROGRESSION_LOOKBACK_WEEKS - i)).isoformat()
            for i in range(_PLAN_PROGRESSION_LOOKBACK_WEEKS)
        ]
    else:
        lookback_starts = []
    week_starts = lookback_starts + plan_week_starts

    easy = _easy_progression(conn, week_starts)
    lookback_workouts: list[WorkoutPoint] = [
        WorkoutPoint(
            date=w["date"], week_start=w["week_start"], title=w["title"], label=w["label"],
            pace_min_per_mile=w["pace_min_per_mile"], distance_mi=w["distance_mi"],
            pace_lo=None, pace_hi=None, zone_name=None,
        )
        for w in (_progression_workout_points(conn, lookback_starts) if lookback_starts else [])
    ]
    workouts = sorted(
        lookback_workouts + _workout_points(conn, active["plan_id"], weeks_in),
        key=lambda p: p["date"],
    )
    checkpoints = _checkpoints_in_window(conn, week_starts)

    return PlanProgressionResponse(
        weeks=week_starts,
        easy=easy,
        workouts=workouts,
        checkpoints=checkpoints,
        captions=ProgressionCaptions(
            easy=_easy_caption(easy),
            workouts=_workout_caption(workouts),
        ),
    )


# Query-param bounds for /api/progression's rolling window — 1 week is the
# degenerate-but-valid floor, 104 (two years) is more history than the chart
# reads sensibly at its fixed width.
_PROGRESSION_DEFAULT_WEEKS = 16
_PROGRESSION_MIN_WEEKS = 1
_PROGRESSION_MAX_WEEKS = 104


class ProgressionWorkoutPoint(WorkoutPoint):
    """WorkoutPoint plus activity_id. /api/progression never has a frozen
    target band or a planned slot to match against (there may be no plan at
    all), so pace_lo/pace_hi/zone_name are always None and title carries the
    activity's own name rather than a planned slot's — same shape otherwise,
    so plan.html's chart renderer runs unmodified regardless of which
    endpoint fed it."""
    activity_id: int


class ProgressionResponse(TypedDict):
    weeks: list[str]  # the last N Mondays ending at the current week — the shared x-axis
    easy: list[WeekEasyOut]  # aligned 1:1 with `weeks`
    workouts: list[ProgressionWorkoutPoint]  # sparse — one entry per synced workout, chronological
    checkpoints: list[CheckpointPoint]  # fitness_checkpoints months landing inside the window
    captions: ProgressionCaptions


def _progression_workout_points(conn: sqlite3.Connection, week_starts: list[str]) -> list[ProgressionWorkoutPoint]:
    """One point per synced workout in [week_starts[0], week_starts[-1] + 6d]
    — the plan-independent twin of _workout_points: no planned-slot matching,
    no frozen target band. Pace prefers the distance-weighted work-lap pace
    (plan_adherence._work_lap_pace, laps.lap_type = 'work'); an activity with
    no classified work laps falls back to its own average pace."""
    if not week_starts:
        return []
    effective = db.effective_run_type_sql("a")
    ph = ",".join("?" * len(_RUN_TYPES))
    start = week_starts[0]
    end = (date.fromisoformat(week_starts[-1]) + timedelta(days=6)).isoformat()
    rows = conn.execute(f"""
        SELECT a.activity_id, a.name, DATE(a.start_date) AS date, a.distance_m, a.average_speed_mps
        FROM activities a
        WHERE a.sport_type IN ({ph}) AND {effective} = 'workout'
          AND DATE(a.start_date) >= ? AND DATE(a.start_date) <= ?
        ORDER BY a.start_date
    """, list(_RUN_TYPES) + [start, end]).fetchall()

    points: list[ProgressionWorkoutPoint] = []
    for r in rows:
        activity_id = int(r["activity_id"])
        work_lap = plan_adherence._work_lap_pace(conn, activity_id)
        if work_lap is not None:
            pace, distance_mi = work_lap
        else:
            distance_mi = float(r["distance_m"] or 0.0) / MILE_M
            pace = 26.8224 / r["average_speed_mps"] if r["average_speed_mps"] else None
        d = r["date"]
        monday = (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())).isoformat()
        points.append(ProgressionWorkoutPoint(
            date=d, week_start=monday, title=r["name"], label=classify_workout(r["name"] or ""),
            pace_min_per_mile=round(pace, 2) if pace is not None else None,
            distance_mi=round(distance_mi, 2),
            pace_lo=None, pace_hi=None, zone_name=None,
            activity_id=activity_id,
        ))
    return points


@router.get("/api/progression")
def get_progression(weeks: int = _PROGRESSION_DEFAULT_WEEKS) -> ProgressionResponse:
    """
    Plan-independent twin of /api/plan-progression — works regardless of plan
    state (no plan, completed-only plan, active plan) so plan.html's Trends
    tab can render the same two charts everywhere. `weeks` is a rolling
    window of the last N Mondays ending at the current week, clamped to
    [_PROGRESSION_MIN_WEEKS, _PROGRESSION_MAX_WEEKS], rather than a plan's
    own week list. Workout points carry no target band (see
    ProgressionWorkoutPoint) — there may be no plan governing them at all.
    """
    conn = _conn()
    n = max(_PROGRESSION_MIN_WEEKS, min(weeks, _PROGRESSION_MAX_WEEKS))
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    week_starts = [(current_monday - timedelta(weeks=n - 1 - i)).isoformat() for i in range(n)]

    easy = _easy_progression(conn, week_starts)
    workouts = _progression_workout_points(conn, week_starts)
    checkpoints = _checkpoints_in_window(conn, week_starts)

    return ProgressionResponse(
        weeks=week_starts,
        easy=easy,
        workouts=workouts,
        checkpoints=checkpoints,
        captions=ProgressionCaptions(
            easy=_easy_caption(easy),
            workouts=_workout_caption(workouts),
        ),
    )


# --- race retrospective ---------------------------------------------------
#
# Closes the loop once the goal race is run (see plan.auto_complete_plan,
# called from sync.py). Everything here reads plan_id off
# plan.get_current_or_recent_plan and 404/nulls when that plan isn't
# 'completed' yet — the forward-looking /api/plan endpoints above already
# work fine for a completed plan (calendar, version history), this is the
# additional data plan.html's retrospective view needs on top.


def _canonical_bucket(bucket: str) -> str | None:
    """Case-insensitive match of a plan's distance_bucket against the
    canonical races.py classify_race_distance() vocabulary (lowercase
    'marathon'/'half', uppercase '5K'/'10K'/...) — the vocabulary
    NOMINAL_METERS and estimate_fitness's `predicted` dict use. The bucket
    casing is inconsistent across the codebase (distance_builds.py's Bucket
    type is Title Case 'Marathon'/'Half'), so this maps either convention
    onto the canonical one rather than assuming a plan's stored casing."""
    target = bucket.strip().casefold()
    for key in NOMINAL_METERS:
        if key.casefold() == target:
            return key
    return None


def _predicted_time_for_race(conn: sqlite3.Connection, race_date: date, category: str) -> tuple[float, str] | None:
    """(predicted finish time in seconds, confidence) for a race's distance
    category, from a live fitness estimate as of the day before race_date —
    same pattern as derive.py's _predicted_pace_for_race (the race-effort
    classification pass), reimplemented here read-only since that helper is
    private to the derive pass and this call is for display, not persisted
    classification. None when the category has no tracked prediction
    (estimate_fitness only projects 5K/10K/half/marathon) or no estimate is
    computable that far back."""
    if category not in NOMINAL_METERS:
        return None
    est = estimate_fitness(conn, race_date - timedelta(days=1))
    if est is None:
        return None
    pace = est["predicted"].get(category)
    if pace is None:
        return None
    time_s = pace * (NOMINAL_METERS[category] / MILE_M) * 60.0
    return round(time_s, 1), est["confidence"]


class RaceResult(TypedDict):
    activity_id: int
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    pace_min_per_mile: float | None


class RetroGoal(TypedDict):
    goal_time_s: int | None
    predicted_time_s: float | None
    predicted_time: str | None
    predicted_confidence: str | None


class FinalAdherence(TypedDict):
    weeks_on_or_close: int
    weeks_completed: int
    pct_of_planned_miles: float | None
    headline: str  # e.g. "14/16 weeks, 91% of planned miles"


class PlanRampSummary(TypedDict):
    weeks: int
    avg_target_miles: float
    peak_target_miles: float


class DetectedBuildSummary(TypedDict):
    found: bool  # whether detect_builds() anchored a build to this race at all
    start: str | None
    weeks: int | None
    avg_mpw: float | None
    peak_3wk_avg: float | None
    bounded_by: str | None
    thin: bool | None


class PlanRetrospectiveResponse(TypedDict):
    plan: PlanRow
    race: RaceResult | None  # None when no synced race actually matched (plan completed some other way)
    goal: RetroGoal
    final_adherence: FinalAdherence | None
    plan_ramp: PlanRampSummary | None
    detected_build: DetectedBuildSummary


def _final_adherence_rollup(conn: sqlite3.Connection, plan_id: int) -> FinalAdherence | None:
    """Final adherence rollup over every plan_adherence row for plan_id:
    weeks on-or-close out of weeks completed, plus the cumulative
    actual-miles/target-miles ratio across the whole plan — the
    "14/16 weeks, 91% of planned miles" headline. None when the plan never
    reached a completed week (e.g. abandoned before week 1 finished)."""
    rows = _adherence_rows(conn, plan_id)
    if not rows:
        return None
    completed = len(rows)
    on_or_close = sum(1 for r in rows if r["band"] in ("on", "close"))
    total_target = sum(float(r["target_miles"] or 0.0) for r in rows)
    total_actual = sum(float(r["actual_miles"] or 0.0) for r in rows)
    pct = round(100.0 * total_actual / total_target) if total_target > 0 else None
    headline = (
        f"{on_or_close}/{completed} weeks, {pct}% of planned miles"
        if pct is not None else f"{on_or_close}/{completed} weeks"
    )
    return FinalAdherence(
        weeks_on_or_close=on_or_close,
        weeks_completed=completed,
        pct_of_planned_miles=float(pct) if pct is not None else None,
        headline=headline,
    )


def _all_week_aggs(conn: sqlite3.Connection) -> list[WeekAgg]:
    """Every calendar week with at least one run across the athlete's whole
    history, Monday-aligned — the same shape api.py's _week_aggs builds
    (duplicated here rather than imported, since api.py imports this router
    and importing back would cycle)."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN {effective} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        WHERE {tc}
        GROUP BY monday
        ORDER BY monday
    """, tp).fetchall()
    return [
        WeekAgg(monday=r["monday"], miles=r["miles"] or 0.0, runs=r["runs"], workouts=r["workouts"] or 0)
        for r in rows
    ]


def _all_race_refs(conn: sqlite3.Connection) -> list[RaceRef]:
    """Every effective-race activity across the athlete's whole history, as
    detect_builds()'s RaceRef input — same duplication rationale as
    _all_week_aggs above."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, name, distance_m
        FROM activities WHERE {tc} AND {effective} = 'race'
        ORDER BY date
    """, tp).fetchall()
    return [
        RaceRef(
            date=r["date"], name=r["name"],
            distance_category=classify_race_distance(r["distance_m"]) or "other",
            distance_m=r["distance_m"],
        )
        for r in rows
        if r["distance_m"] is not None
    ]


def _detected_build_for_race(conn: sqlite3.Connection, race_date: str) -> Build | None:
    """The detect_builds() (builds.py — the same race-anchored detector
    builds.html indexes) build anchored to race_date, if any. Most short
    races never anchor a detected build (below BUILD_ANCHOR_MIN_M) — the
    caller reports `found: false` rather than falling back to a fixed
    window, since this comparison is explicitly "what the build detector
    saw", not a substitute training-window stat."""
    weeks = _all_week_aggs(conn)
    if not weeks:
        return None
    periods, _gaps = detect_periods(weeks)
    if not periods:
        return None
    builds = detect_builds(weeks, _all_race_refs(conn), periods)
    return next((b for b in builds if b["race"]["date"] == race_date), None)


@router.get("/api/plan-retrospective")
def get_plan_retrospective() -> PlanRetrospectiveResponse | None:
    """
    The closed-loop view for a completed plan: the final adherence rollup,
    the matching race result, that race's goal vs a live pre-race fitness
    prediction (estimate_fitness as of the day before the race — same
    pattern as derive.py's _predicted_pace_for_race), and the plan's target
    ramp vs what detect_builds() independently saw for the same race.

    Reads plan.get_current_or_recent_plan like the endpoints above, but
    returns null unless that plan's status is actually 'completed' —
    plan.html only renders this view once /api/plan reports a completed
    plan, so an active plan simply has nothing here yet.
    """
    conn = _conn()
    current = plan.get_current_or_recent_plan(conn)
    if current is None or current["status"] != "completed":
        return None

    match = plan.find_completing_race(conn, current)
    race: RaceResult | None = None
    category: str | None = None
    if match is not None:
        category = classify_race_distance(match["distance_m"])
        pace = (
            round((match["moving_time_s"] / 60.0) / (match["distance_m"] / MILE_M), 2)
            if match["moving_time_s"] and match["distance_m"] else None
        )
        race = RaceResult(
            activity_id=match["activity_id"], name=match["name"], date=match["date"],
            finish_time_s=match["moving_time_s"], finish_time=fmt_time(match["moving_time_s"]),
            pace_min_per_mile=pace,
        )
    if category is None:
        category = _canonical_bucket(current["distance_bucket"])

    predicted_time_s: float | None = None
    predicted_confidence: str | None = None
    if category is not None:
        race_dt = date.fromisoformat(current["race_date"])
        predicted = _predicted_time_for_race(conn, race_dt, category)
        if predicted is not None:
            predicted_time_s, predicted_confidence = predicted

    goal = RetroGoal(
        goal_time_s=current["goal_time_s"],
        predicted_time_s=predicted_time_s,
        predicted_time=fmt_time(round(predicted_time_s)) if predicted_time_s is not None else None,
        predicted_confidence=predicted_confidence,
    )

    final_adherence = _final_adherence_rollup(conn, current["plan_id"])

    latest = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? ORDER BY version_n DESC LIMIT 1",
        [current["plan_id"]],
    ).fetchone()
    bundle = plan.get_version(conn, int(latest["version_id"])) if latest is not None else None
    weeks_in = bundle["weeks"] if bundle is not None else []
    plan_ramp: PlanRampSummary | None = None
    if weeks_in:
        # Weeks authored with no mileage target (deliberately unspecified) don't
        # contribute to the ramp average/peak — there's no target to average.
        targets = [w["target_miles"] for w in weeks_in if w["target_miles"] is not None]
        if targets:
            plan_ramp = PlanRampSummary(
                weeks=len(weeks_in),
                avg_target_miles=round(sum(targets) / len(targets), 1),
                peak_target_miles=round(max(targets), 1),
            )

    race_date_for_build = race["date"] if race is not None else current["race_date"]
    detected = _detected_build_for_race(conn, race_date_for_build)
    detected_build = DetectedBuildSummary(
        found=detected is not None,
        start=detected["start"] if detected else None,
        weeks=detected["weeks"] if detected else None,
        avg_mpw=detected["avg_mpw"] if detected else None,
        peak_3wk_avg=detected["peak_3wk_avg"] if detected else None,
        bounded_by=detected["bounded_by"] if detected else None,
        thin=detected["thin"] if detected else None,
    )

    return PlanRetrospectiveResponse(
        plan=current,
        race=race,
        goal=goal,
        final_adherence=final_adherence,
        plan_ramp=plan_ramp,
        detected_build=detected_build,
    )


# --- readiness --------------------------------------------------------------
#
# "Race is in N weeks. Am I ready?" — the forward-looking twin of the
# retrospective above, for an ACTIVE plan only (a completed plan renders the
# retrospective in the same slot). Three evidence blocks: goal vs current
# fitness (estimate_fitness + the plan window's monthly checkpoints), the
# in-progress block vs a completed reference build at the same distance
# (weeks-out aligned, both sides cut at the same point — never a partial
# block vs a complete one), and session execution (matched workouts in their
# frozen target band + longest run vs the plan's peak long-run target).


# source_tier -> basis vocabulary: race-confirmed (tier 1, an actual raced
# effort), workout-anchored (tier 2, work-lap paces), training-floor (tier 3,
# training-pace envelope). Absolute per estimate, unlike the confidence word,
# which also encodes recency.
_READINESS_BASIS_BY_TIER: dict[int, str] = {1: "race-confirmed", 2: "workout-anchored", 3: "training-floor"}

# fitness_checkpoints pace column per canonical race category. Only the four
# distances estimate_fitness projects are tracked; a plan at any other
# distance (50K, Other) gets no checkpoint mini-line and no predicted time —
# the goal block degrades to nulls rather than borrowing a proxy distance.
_CHECKPOINT_PACE_COL: dict[str, str] = {
    "5K": "pace_5k", "10K": "pace_10k", "half": "pace_half", "marathon": "pace_marathon",
}

# Reference candidates with less window overlap than this against the elapsed
# plan carry too little shared signal to compare cumulatively at all.
_MIN_OVERLAP_WEEKS = 3

# How many completed weeks back the session-execution window reaches.
_SESSION_WINDOW_WEEKS = 3


def _builds_bucket(bucket: str) -> Bucket | None:
    """Case-insensitive match of a plan's distance_bucket against
    distance_builds.py's Title Case Bucket vocabulary — same casing
    reconciliation as _canonical_bucket above, pointed at the other
    convention."""
    target = bucket.strip().casefold()
    for key in get_args(Bucket):
        if key.casefold() == target:
            return cast(Bucket, key)
    return None


class ReadinessCheckpoint(TypedDict):
    month: str
    pace_min_per_mile: float


class ReadinessGoal(TypedDict):
    """Goal vs current fitness at the plan's race distance. predicted_* /
    basis / confidence are None when estimate_fitness tracks no prediction at
    that distance (see _CHECKPOINT_PACE_COL) or no estimate is computable;
    goal_* are None when the plan was authored without a goal time."""
    goal_time_s: int | None
    goal_pace_min_per_mile: float | None
    predicted_time_s: float | None
    predicted_pace_min_per_mile: float | None
    basis: str | None
    confidence: str | None
    trend_s_per_mi: float | None  # last minus first in-window checkpoint, sec/mi (negative = faster); None below 2 checkpoints
    checkpoints: list[ReadinessCheckpoint]  # in-window months with a tracked pace at the race distance


class ReadinessWeekPoint(TypedDict):
    offset: int  # weeks to race (0 = race week, negative = before), Monday-aligned
    miles: float


class ReadinessBlockStats(TypedDict):
    """Cumulative stats over the shared comparison offsets [-W, -k] (see
    get_plan_readiness) — identical window on both sides, weekly points dense
    over that range (a synced week with no runs is a real 0.0, never a gap)."""
    avg_mpw: float
    long_runs: int
    peak_week: float | None  # None when no week in the range has any runs
    weekly: list[ReadinessWeekPoint]


class ReadinessReference(ReadinessBlockStats):
    overlap_weeks: int


class ReadinessCandidate(TypedDict):
    name: str | None
    race_date: str
    result_s: int | None
    is_pr: bool  # the default reference — exactly one candidate carries it


class ReadinessBuilds(TypedDict):
    bucket: str  # distance_builds Bucket casing — the vocabulary reference-choice persistence keys on
    candidates: list[ReadinessCandidate]
    through_weeks_out: int  # k — both sides cut at this weeks-out point
    window_weeks: int  # W = min(plan length, bucket build-window length)
    current: ReadinessBlockStats
    by_date: dict[str, ReadinessReference]  # every candidate's aligned stats, so flipping needs no refetch


class ReadinessSessionPoint(TypedDict):
    date: str
    pace_min_per_mile: float
    pace_lo: float  # frozen target band from the version governing that week
    pace_hi: float
    in_band: bool


class ReadinessSessions(TypedDict):
    workouts_in_band: int
    workouts_total: int
    window_weeks: int  # completed weeks actually covered (< _SESSION_WINDOW_WEEKS early in a plan)
    points: list[ReadinessSessionPoint]
    longest_run_mi: float | None  # longest single run over [plan start, sync cutoff]
    plan_peak_long_run_miles: float | None  # max weekly long-run mileage target across the plan


class PlanReadinessResponse(TypedDict):
    weeks_to_race: int
    race_date: str
    goal: ReadinessGoal
    builds: ReadinessBuilds | None  # None when no comparable reference exists (first race at the distance, or too little overlap yet)
    sessions: ReadinessSessions | None  # None until the plan has a completed sync-covered week


def _weekly_offset_miles(
    conn: sqlite3.Connection, anchor_monday: date, weeks_before: int, start: str, end: str
) -> dict[int, float]:
    """Weekly miles keyed by week offset from race day (0 = race week),
    over [start, end] inclusive. anchor_monday must be the race-week Monday
    minus weeks_before weeks, so all julianday differences are positive and
    CAST truncates toward zero correctly — the app-wide offset convention
    (see distance_builds.py). Only weeks with at least one run appear."""
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT
            CAST((julianday(DATE(start_date)) - julianday(?)) / 7.0 AS INTEGER) - {weeks_before} AS week_offset,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles
        FROM activities
        WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        GROUP BY week_offset
    """, [anchor_monday.isoformat()] + tp + [start, end]).fetchall()
    return {int(r["week_offset"]): float(r["miles"] or 0.0) for r in rows}


def _long_run_count(conn: sqlite3.Connection, start: str, end: str) -> int:
    """Count of effective-type long runs over [start, end] inclusive."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    row = conn.execute(f"""
        SELECT COUNT(*) AS n FROM activities
        WHERE {tc} AND {effective} = 'long_run'
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
    """, tp + [start, end]).fetchone()
    return int(row["n"] or 0)


def _block_stats(
    conn: sqlite3.Connection, race_monday: date, weeks_before: int, window_w: int, k: int, end: str
) -> ReadinessBlockStats:
    """One side of the build comparison: cumulative stats over offsets
    [-window_w, -k], dates [race_monday - window_w weeks, end]. The average
    divides by the full offset count, so a zero-mileage week inside the range
    drags it down the way it should — the range is entirely sync-covered by
    construction, so absence there means no running, not no data."""
    anchor = race_monday - timedelta(weeks=weeks_before)
    start = (race_monday - timedelta(weeks=window_w)).isoformat()
    by_offset = _weekly_offset_miles(conn, anchor, weeks_before, start, end)
    offsets = range(-window_w, -k + 1)
    total = sum(by_offset.get(o, 0.0) for o in offsets)
    observed = [by_offset[o] for o in offsets if o in by_offset]
    return ReadinessBlockStats(
        avg_mpw=round(total / len(offsets), 1) if len(offsets) else 0.0,
        long_runs=_long_run_count(conn, start, end),
        peak_week=max(observed) if observed else None,
        weekly=[ReadinessWeekPoint(offset=o, miles=by_offset.get(o, 0.0)) for o in offsets],
    )


def _race_efforts(conn: sqlite3.Connection) -> dict[str, str | None]:
    """race_effort keyed by race date, for splitting raced from casual
    candidates — get_distance_builds' rows don't carry the effort column."""
    effective = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(
        f"SELECT DATE(start_date) AS date, race_effort FROM activities WHERE {tc} AND {effective} = 'race'",
        tp,
    ).fetchall()
    return {r["date"]: r["race_effort"] for r in rows}


def _readiness_goal(
    conn: sqlite3.Connection, active: PlanRow, weeks_in: list[PlanWeekRow], today: date
) -> ReadinessGoal:
    canonical = _canonical_bucket(active["distance_bucket"])
    nominal_mi = NOMINAL_METERS[canonical] / MILE_M if canonical is not None else None

    goal_time_s = active["goal_time_s"]
    goal_pace = (
        round((goal_time_s / 60.0) / nominal_mi, 2)
        if goal_time_s is not None and nominal_mi is not None else None
    )

    predicted_pace: float | None = None
    predicted_time_s: float | None = None
    basis: str | None = None
    confidence: str | None = None
    est = estimate_fitness(conn, today)
    if est is not None and canonical is not None and nominal_mi is not None:
        pace = est["predicted"].get(canonical)
        if pace is not None:
            predicted_pace = pace
            predicted_time_s = round(pace * nominal_mi * 60.0, 1)
            confidence = est["confidence"]
            if est["sources"]:
                basis = _READINESS_BASIS_BY_TIER.get(est["sources"][0]["tier"])

    checkpoints: list[ReadinessCheckpoint] = []
    col = _CHECKPOINT_PACE_COL.get(canonical) if canonical is not None else None
    if col is not None and weeks_in:
        rows = conn.execute(
            f"SELECT month, {col} AS pace FROM fitness_checkpoints "
            f"WHERE month >= ? AND month <= ? AND {col} IS NOT NULL ORDER BY month",
            [weeks_in[0]["week_start"][:7], weeks_in[-1]["week_start"][:7]],
        ).fetchall()
        checkpoints = [ReadinessCheckpoint(month=r["month"], pace_min_per_mile=float(r["pace"])) for r in rows]

    trend_s_per_mi = (
        round((checkpoints[-1]["pace_min_per_mile"] - checkpoints[0]["pace_min_per_mile"]) * 60.0, 1)
        if len(checkpoints) >= 2 else None
    )

    return ReadinessGoal(
        goal_time_s=goal_time_s,
        goal_pace_min_per_mile=goal_pace,
        predicted_time_s=predicted_time_s,
        predicted_pace_min_per_mile=predicted_pace,
        basis=basis,
        confidence=confidence,
        trend_s_per_mi=trend_s_per_mi,
        checkpoints=checkpoints,
    )


def _readiness_builds(
    conn: sqlite3.Connection,
    active: PlanRow,
    plan_weeks_before: int,
    race_monday: date,
    k: int,
    cutoff: date,
) -> ReadinessBuilds | None:
    """The in-progress block vs completed builds at the same distance, aligned
    by weeks-out. Candidates: the builds behind the 3 fastest raced efforts
    plus the most recent completed one, deduped; the fastest raced (the PR
    build) is the default. Both sides' cumulative stats cover the same
    offsets [-W, -k] — the current block never gets judged partial-vs-whole,
    and a reference never gets credit for weeks the plan hasn't reached.
    None when nothing comparable exists: no completed race at the distance,
    or fewer than _MIN_OVERLAP_WEEKS shared weeks so far (the bucket's build
    window is fixed, so overlap is uniform across candidates)."""
    bucket = _builds_bucket(active["distance_bucket"])
    if bucket is None:
        return None
    rows = get_distance_builds(bucket)
    if not rows:
        return None

    ref_weeks = rows[0]["build_weeks"]
    window_w = min(plan_weeks_before, ref_weeks)
    overlap = window_w - k + 1
    if overlap < _MIN_OVERLAP_WEEKS:
        return None

    efforts = _race_efforts(conn)
    raced = sorted(
        (r for r in rows if efforts.get(r["date"]) == "raced" and r["finish_time_s"] is not None),
        key=lambda r: cast(int, r["finish_time_s"]),
    )
    picks = raced[:3] + [rows[-1]]  # rows are date-ascending, so rows[-1] is the most recent
    seen: set[str] = set()
    deduped = [r for r in picks if not (r["date"] in seen or seen.add(r["date"]))]
    pr_date = raced[0]["date"] if raced else deduped[0]["date"]

    by_date: dict[str, ReadinessReference] = {}
    candidates: list[ReadinessCandidate] = []
    for r in deduped:
        ref_race_dt = date.fromisoformat(r["date"])
        ref_monday = ref_race_dt - timedelta(days=ref_race_dt.weekday())
        ref_end = (ref_monday - timedelta(weeks=k) + timedelta(days=6)).isoformat()
        stats = _block_stats(conn, ref_monday, r["build_weeks"], window_w, k, ref_end)
        by_date[r["date"]] = ReadinessReference(**stats, overlap_weeks=overlap)
        candidates.append(ReadinessCandidate(
            name=r["name"], race_date=r["date"], result_s=r["finish_time_s"], is_pr=r["date"] == pr_date,
        ))

    current = _block_stats(conn, race_monday, plan_weeks_before, window_w, k, cutoff.isoformat())

    return ReadinessBuilds(
        bucket=bucket,
        candidates=candidates,
        through_weeks_out=k,
        window_weeks=window_w,
        current=current,
        by_date=by_date,
    )


def _readiness_sessions(
    conn: sqlite3.Connection,
    active: PlanRow,
    weeks_in: list[PlanWeekRow],
    plan_start: date,
    cutoff: date,
) -> ReadinessSessions | None:
    """Session execution over the last _SESSION_WINDOW_WEEKS completed
    (sync-covered) weeks: synced workouts matched to their planned slots with
    plan_adherence's matching — so this and the adherence score never
    disagree about which run was the workout — each judged against the frozen
    band from the version that governed its week, with the same
    PACE_TOLERANCE slack the weekly band scoring applies. Trail slots are
    matched but never judged (grade voids road pace bands), same as
    adherence. None until the plan has a completed week."""
    completed = [
        w for w in weeks_in
        if date.fromisoformat(w["week_start"]) + timedelta(days=6) < cutoff
    ]
    if not completed:
        return None
    window = completed[-_SESSION_WINDOW_WEEKS:]

    points: list[ReadinessSessionPoint] = []
    for w in window:
        week_start = date.fromisoformat(w["week_start"])
        week_end = week_start + timedelta(days=6)
        governing = plan.current_version_for_week(conn, active["plan_id"], week_start)
        if governing is None:
            continue
        day_rows = [d for d in governing["days"] if w["week_start"] <= d["date"] <= week_end.isoformat()]
        slots = plan_adherence._planned_workout_slots(day_rows)
        actuals = plan_adherence._gather_actuals(conn, week_start, week_end)
        for slot, pace in plan_adherence._match_workouts(slots, actuals["workout_paces"]):
            if slot["terrain"] == "trail":
                continue
            lo, hi, actual = slot["pace_lo"], slot["pace_hi"], pace["pace_min_per_mile"]
            if lo is None or hi is None or actual is None:
                continue
            points.append(ReadinessSessionPoint(
                date=pace["date"],
                pace_min_per_mile=round(actual, 2),
                pace_lo=lo,
                pace_hi=hi,
                in_band=plan_adherence._pace_delta_s(actual, lo, hi) == 0.0,
            ))
    points.sort(key=lambda p: p["date"])

    tc, tp = _type_clause()
    longest = conn.execute(
        f"SELECT MAX(distance_m) AS d FROM activities WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?",
        tp + [plan_start.isoformat(), cutoff.isoformat()],
    ).fetchone()
    longest_run_mi = round(float(longest["d"]) / MILE_M, 1) if longest is not None and longest["d"] else None

    lr_targets = [w["target_long_run_miles"] for w in weeks_in if w["target_long_run_miles"] is not None]

    return ReadinessSessions(
        workouts_in_band=sum(1 for p in points if p["in_band"]),
        workouts_total=len(points),
        window_weeks=len(window),
        points=points,
        longest_run_mi=longest_run_mi,
        plan_peak_long_run_miles=round(max(lr_targets), 1) if lr_targets else None,
    )


@router.get("/api/plan-readiness")
def get_plan_readiness() -> PlanReadinessResponse | None:
    """
    "Am I ready?" evidence for the ACTIVE plan — null for a completed plan
    (the retrospective owns that slot) or when no plan exists.

    Alignment is by weeks-out, never calendar: with k = weeks to race and
    W = min(plan length, the bucket's build-window length), both the current
    block and every reference build report cumulative stats over the same
    Monday-aligned offsets [-W, -k]. k is computed at the sync cutoff
    (min(today, last_sync_at's date)) rather than the calendar today, so a
    stale sync narrows the compared range instead of reading absent weeks as
    zeros — the same clamp _today_blocks applies at week scale.

    All candidates' aligned stats ship in one payload (builds.by_date) so
    flipping the reference is purely client-side.
    """
    conn = _conn()
    active = plan.get_current_or_recent_plan(conn)
    if active is None or active["status"] != "active":
        return None

    latest = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? ORDER BY version_n DESC LIMIT 1",
        [active["plan_id"]],
    ).fetchone()
    bundle = plan.get_version(conn, int(latest["version_id"])) if latest is not None else None
    weeks_in = bundle["weeks"] if bundle is not None else []
    if not weeks_in:
        return None

    today = date.today()
    last_sync_at = db.get_last_sync_at(conn)
    sync_date = date.fromisoformat(last_sync_at[:10]) if last_sync_at else today
    cutoff = min(today, sync_date)

    race_dt = date.fromisoformat(active["race_date"])
    race_monday = race_dt - timedelta(days=race_dt.weekday())
    current_monday = today - timedelta(days=today.weekday())
    weeks_to_race = max(0, (race_monday - current_monday).days // 7)

    plan_start = date.fromisoformat(weeks_in[0]["week_start"])
    plan_weeks_before = max(0, (race_monday - plan_start).days // 7)
    cutoff_monday = cutoff - timedelta(days=cutoff.weekday())
    k = max(0, (race_monday - cutoff_monday).days // 7)

    return PlanReadinessResponse(
        weeks_to_race=weeks_to_race,
        race_date=active["race_date"],
        goal=_readiness_goal(conn, active, weeks_in, today),
        builds=_readiness_builds(conn, active, plan_weeks_before, race_monday, k, cutoff),
        sessions=_readiness_sessions(conn, active, weeks_in, plan_start, cutoff),
    )
