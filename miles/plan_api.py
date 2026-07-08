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
from typing import cast

from typing_extensions import TypedDict

from fastapi import APIRouter, HTTPException

from . import db, plan, plan_adherence
from .builds import Build, RaceRef, detect_builds
from .derive import ensure_derived
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
    target_miles: float
    target_workouts: int
    target_long_run_miles: float | None
    phase: str
    note: str | None
    actual_miles: float
    actual_workouts: int
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
    target: DayTarget | None


class ThisWeekStat(TypedDict):
    week_start: str
    target_miles: float
    actual_miles: float
    target_workouts: int
    actual_workouts: int


class GoalStat(TypedDict):
    goal_time_s: int
    equivalent_time_s: float | None
    confidence: str | None


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


def _day_out(d: PlanDayRow) -> PlanDayOut:
    target = cast(DayTarget, json.loads(d["target_json"])) if d["target_json"] else None
    return PlanDayOut(
        date=d["date"], seq=d["seq"], slot=d["slot"], title=d["title"],
        target_miles=d["target_miles"], target=target,
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
        for w in weeks_in:
            actual_miles, actual_workouts = actual_weeks.get(w["week_start"], (0.0, 0))
            weeks_out.append(PlanWeekOut(
                week_start=w["week_start"],
                target_miles=w["target_miles"],
                target_workouts=w["target_workouts"],
                target_long_run_miles=w["target_long_run_miles"],
                phase=w["phase"],
                note=w["note"],
                actual_miles=actual_miles,
                actual_workouts=actual_workouts,
                is_current=w["week_start"] == current_monday,
                is_future=w["week_start"] > current_monday,
            ))
            if w["week_start"] == current_monday:
                this_week = ThisWeekStat(
                    week_start=w["week_start"],
                    target_miles=w["target_miles"],
                    actual_miles=actual_miles,
                    target_workouts=w["target_workouts"],
                    actual_workouts=actual_workouts,
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
    actual_miles: float | None
    mileage_ratio: float | None
    target_workouts: int | None
    actual_workouts: int | None
    target_long_run_miles: float | None
    long_run_done: bool | None
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
    week's contemporaneous target_miles/target_workouts/target_long_run_miles
    — shared by get_plan_adherence and the retrospective's final-adherence
    rollup (get_plan_retrospective) so both read off one query."""
    return conn.execute("""
        SELECT
            pa.week_start, pa.version_n_used, pa.actual_miles, pa.actual_workouts,
            pa.long_run_done, pa.mileage_ratio, pa.workout_pace_delta_s, pa.band, pa.flags_json,
            pw.target_miles, pw.target_workouts, pw.target_long_run_miles
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
            actual_miles=r["actual_miles"],
            mileage_ratio=r["mileage_ratio"],
            target_workouts=r["target_workouts"],
            actual_workouts=r["actual_workouts"],
            target_long_run_miles=r["target_long_run_miles"],
            long_run_done=bool(r["long_run_done"]) if r["long_run_done"] is not None else None,
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


def _checkpoints_in_window(conn: sqlite3.Connection, weeks_in: list[PlanWeekRow]) -> list[CheckpointPoint]:
    """fitness_checkpoints months landing inside the plan window — background
    context for the workout-pace chart, per training.html's monthly-checkpoint
    treatment."""
    if not weeks_in:
        return []
    start_month = weeks_in[0]["week_start"][:7]
    end_month = weeks_in[-1]["week_start"][:7]
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


def _workout_caption(points: list[WorkoutPoint]) -> str:
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


@router.get("/api/plan-progression")
def get_plan_progression() -> PlanProgressionResponse | None:
    """
    The "is it working" data, scoped to the active plan's window — the other
    two of the three progression charts (weekly mileage vs ramp already exists
    from /api/plan + /api/plan-adherence's bands; see the module comment
    above).

    `weeks` is the full plan-window Monday list from the latest version (the
    same list /api/plan reports) — the shared, continuous x-axis for the easy
    HR/pace chart, so a week with no easy runs synced renders as a gap, never
    a missing category.

    `easy`: one row per week in `weeks`; see _easy_progression.

    `workouts`: one point per synced workout matched to a planned workout slot
    (plan_adherence's matching, reused); see _workout_points.

    `checkpoints`: monthly 5K-pace fitness_checkpoints rows landing inside the
    plan window, for the workout chart's background reference.

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
    week_starts = [w["week_start"] for w in weeks_in]

    easy = _easy_progression(conn, week_starts)
    workouts = _workout_points(conn, active["plan_id"], weeks_in)
    checkpoints = _checkpoints_in_window(conn, weeks_in)

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
        targets = [w["target_miles"] for w in weeks_in]
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
