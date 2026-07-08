import logging
import sqlite3
import click
import subprocess as _subprocess
import uvicorn
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .build_paces import PaceClaim, pace_claims
from .classifier import LAP_MIN_DISTANCE_M, LAP_MIN_MOVING_TIME_S
from .distance_builds import (
    Bucket,
    router as distance_builds_router,
    _BUILD_WEEKS as _DISTANCE_BUILD_WEEKS,
    _build_start as _distance_build_start,
    _DISTANCE_CATEGORY,
)
from .fitness_api import router as fitness_api_router
from .builds import Build, RaceRef, detect_builds
from .derive import ensure_derived
from .format import fmt_pace, fmt_time
from .periods import Gap, Period, WeekAgg, is_active, zero_fill, detect_periods
from .races import MARATHON_MAX_M, MARATHON_MIN_M, classify_race_distance, race_rows

app = FastAPI(title="miles")

_logger = logging.getLogger(__name__)
_sync_proc: _subprocess.Popen[bytes] | None = None
_REPO_ROOT = Path(__file__).parent.parent

_BUILD_WEEKS = 12
_RUN_TYPES = ("Run", "TrailRun", "VirtualRun")
_STATIC = Path(__file__).parent / "static"
_WORKBOOKS = Path("data/workbooks")
_WORKBOOKS.mkdir(parents=True, exist_ok=True)

# Inverse of distance_builds.py's Bucket -> category map, so build-detail can
# find a race's fixed-window bucket from race_rows()'s distance_category.
# Categories with no dedicated bucket (15K/10M/30K) fall back to "Other".
_CATEGORY_TO_BUCKET: dict[str, Bucket] = {cat: bucket for bucket, cat in _DISTANCE_CATEGORY.items()}


class RunTypeStat(TypedDict):
    runs: int
    total_miles: float
    avg_miles: float
    avg_hr: float | None
    avg_pace_min_per_mile: float | None


class BuildStat(TypedDict):
    start: str
    weeks: int
    total_miles: float
    avg_mpw: float
    peak_week: float | None
    peak_3wk_avg: float | None
    by_type: dict[str, RunTypeStat]
    pace_claims: dict[str, PaceClaim | None]


class MarathonRow(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    distance_miles: float | None
    pace_min_per_mile: float | None
    build: BuildStat


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    ensure_derived(conn)
    return conn


def _type_clause() -> tuple[str, list[str]]:
    ph = ",".join("?" * len(_RUN_TYPES))
    return f"sport_type IN ({ph})", list(_RUN_TYPES)


def _week_aggs(conn: sqlite3.Connection) -> list[WeekAgg]:
    """Every calendar week with at least one run, Monday-aligned. Shared by
    weekly-history and build-detail so both feed the same detect_builds() input."""
    effective_run_type = db.effective_run_type_sql()
    tc, tp = _type_clause()
    week_rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN {effective_run_type} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        WHERE {tc}
        GROUP BY monday
        ORDER BY monday
    """, tp).fetchall()
    return [
        {"monday": r["monday"], "miles": r["miles"] or 0.0, "runs": r["runs"], "workouts": r["workouts"] or 0}
        for r in week_rows
    ]


class _RaceActivityRow(TypedDict):
    date: str
    name: str | None
    distance_m: float | None
    race_effort: str | None


def _race_activity_rows(conn: sqlite3.Connection) -> list[_RaceActivityRow]:
    """Raw race rows (date/name/distance_m/effort) — the shared source for
    both weekly-history's RaceMarker list and build-detail's RaceRef list."""
    effective_run_type = db.effective_run_type_sql()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, name, distance_m, race_effort
        FROM activities
        WHERE {tc} AND {effective_run_type} = 'race'
        ORDER BY date
    """, tp).fetchall()
    return [
        {"date": r["date"], "name": r["name"], "distance_m": r["distance_m"], "race_effort": r["race_effort"]}
        for r in rows
    ]


def _race_refs(rows: list[_RaceActivityRow]) -> list[RaceRef]:
    return [
        RaceRef(
            date=r["date"],
            name=r["name"],
            distance_category=classify_race_distance(r["distance_m"]) or "other",
            distance_m=r["distance_m"],
        )
        for r in rows
        if r["distance_m"] is not None
    ]


@app.get("/api/marathons")
def get_marathons(build_weeks: int = _BUILD_WEEKS) -> list[MarathonRow]:
    """
    All marathon race results with stats for the build_weeks-week training
    block that preceded each one. Sorted by date ascending.
    """
    conn = _conn()
    tc, tp = _type_clause()
    effective_run_type = db.effective_run_type_sql()

    races = conn.execute("""
        SELECT
            name,
            DATE(start_date) AS race_date,
            ROUND(distance_m / 1609.34, 2) AS distance_miles,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile
        FROM activities
        WHERE run_type = 'race'
          AND distance_m BETWEEN ? AND ?
        ORDER BY race_date
    """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchall()

    out: list[MarathonRow] = []
    for race in races:
        race_date: str = race["race_date"]
        build_start: str = _distance_build_start(race_date, build_weeks).isoformat()

        by_type_rows = conn.execute(f"""
            SELECT
                {effective_run_type} AS run_type,
                COUNT(*)                                     AS runs,
                ROUND(SUM(distance_m) / 1609.34, 2)         AS total_miles,
                ROUND(AVG(distance_m) / 1609.34, 2)         AS avg_miles,
                ROUND(AVG(average_heartrate), 1)             AS avg_hr,
                CASE WHEN AVG(average_speed_mps) > 0
                     THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                     ELSE NULL END                           AS avg_pace_min_per_mile
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
            GROUP BY {effective_run_type}
            ORDER BY 1
        """, tp + [build_start, race_date]).fetchall()

        totals = conn.execute(f"""
            SELECT ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
        """, tp + [build_start, race_date]).fetchone()

        peak = conn.execute(f"""
            WITH weekly AS (
                SELECT
                    strftime('%Y-W%W', start_date) AS week,
                    ROUND(SUM(distance_m) / 1609.34, 2) AS miles
                FROM activities
                WHERE {tc}
                  AND DATE(start_date) >= ?
                  AND DATE(start_date) < ?
                GROUP BY week
            ),
            rolling AS (
                SELECT
                    miles,
                    ROUND(AVG(miles) OVER (
                        ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
                    ), 1) AS r3
                FROM weekly
            )
            SELECT MAX(miles) AS peak_week, MAX(r3) AS peak_3wk_avg FROM rolling
        """, tp + [build_start, race_date]).fetchone()

        total_miles: float = totals["total_miles"] or 0.0

        out.append(MarathonRow(
            name=race["name"],
            date=race_date,
            finish_time_s=race["moving_time_s"],
            finish_time=fmt_time(race["moving_time_s"]),
            distance_miles=race["distance_miles"],
            pace_min_per_mile=race["pace_min_per_mile"],
            build=BuildStat(
                start=build_start,
                weeks=build_weeks,
                total_miles=total_miles,
                avg_mpw=round(total_miles / build_weeks, 1),
                peak_week=peak["peak_week"] if peak else None,
                peak_3wk_avg=peak["peak_3wk_avg"] if peak else None,
                by_type={
                    row["run_type"]: RunTypeStat(
                        runs=row["runs"],
                        total_miles=row["total_miles"],
                        avg_miles=row["avg_miles"],
                        avg_hr=row["avg_hr"],
                        avg_pace_min_per_mile=row["avg_pace_min_per_mile"],
                    )
                    for row in by_type_rows
                    if row["run_type"] is not None
                },
                pace_claims=pace_claims(conn, build_start, race_date),
            ),
        ))

    return out


class WeekPoint(TypedDict):
    offset: int
    miles: float


class MarathonWeeks(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    weeks: list[WeekPoint]


@app.get("/api/marathon-weeks")
def get_marathon_weeks(build_weeks: int = _BUILD_WEEKS) -> list[MarathonWeeks]:
    """
    Weekly mileage for each marathon build, with each week expressed as an
    offset from race day (0 = race week, -1 = one week before, etc.).
    Race day itself is excluded so week 0 shows only taper runs.
    """
    conn = _conn()
    tc, tp = _type_clause()

    races = conn.execute("""
        SELECT name, DATE(start_date) AS race_date, moving_time_s
        FROM activities
        WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
        ORDER BY race_date
    """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchall()

    out: list[MarathonWeeks] = []
    for race in races:
        race_date: str = race["race_date"]
        # Align to Monday so every week is Mon–Sun and no week is partially cut off.
        # race_week_monday = the Monday on or before race_date.
        race_dt = date.fromisoformat(race_date)
        race_week_monday = race_dt - timedelta(days=race_dt.weekday())
        build_start = (race_week_monday - timedelta(weeks=build_weeks)).isoformat()

        # Offset formula anchored to build_start (always a Monday):
        #   0  = race week (race_week_monday through race_date, inclusive)
        #  -1  = week before, … -12 = first week of build
        # Using build_start as anchor keeps all differences positive so CAST
        # truncates correctly without needing floor division.
        week_rows = conn.execute(f"""
            SELECT
                CAST((julianday(DATE(start_date)) - julianday(?)) / 7.0 AS INTEGER) - {build_weeks} AS week_offset,
                ROUND(SUM(distance_m) / 1609.34, 2) AS miles
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ?
              AND DATE(start_date) <= ?
            GROUP BY week_offset
            ORDER BY week_offset
        """, [build_start] + tp + [build_start, race_date]).fetchall()

        out.append(MarathonWeeks(
            name=race["name"],
            date=race_date,
            finish_time_s=race["moving_time_s"],
            finish_time=fmt_time(race["moving_time_s"]),
            weeks=[WeekPoint(offset=row["week_offset"], miles=row["miles"]) for row in week_rows],
        ))

    return out


class HistoryWeek(TypedDict):
    monday: str
    miles: float
    runs: int


class RaceMarker(TypedDict):
    date: str
    name: str | None
    distance_category: str
    effort: str | None


class WeeklyHistory(TypedDict):
    weeks: list[HistoryWeek]
    periods: list[Period]
    gaps: list[Gap]
    builds: list[Build]
    races: list[RaceMarker]


@app.get("/api/weekly-history")
def get_weekly_history() -> WeeklyHistory:
    """
    Every week of running history, zero-filled, plus detected training periods,
    gaps, race-anchored builds within them, and every race with its effort label.
    """
    conn = _conn()

    weeks = _week_aggs(conn)
    filled_weeks = zero_fill(weeks)
    periods, gaps = detect_periods(weeks)

    race_activity_rows = _race_activity_rows(conn)
    races = [
        RaceMarker(
            date=r["date"],
            name=r["name"],
            distance_category=classify_race_distance(r["distance_m"]) or "other",
            effort=r["race_effort"],
        )
        for r in race_activity_rows
    ]
    builds = detect_builds(weeks, _race_refs(race_activity_rows), periods) if periods else []

    return WeeklyHistory(
        weeks=[HistoryWeek(monday=w["monday"], miles=w["miles"], runs=w["runs"]) for w in filled_weeks],
        periods=periods,
        gaps=gaps,
        builds=builds,
        races=races,
    )


class BuildDetailRace(TypedDict):
    name: str | None
    date: str
    distance_category: str
    finish_time: str
    finish_time_s: int | None
    pace_min_per_mile: float | None
    effort: str | None
    id: int


class BuildDetailStat(TypedDict):
    start: str
    weeks: int
    bounded_by: str | None
    avg_mpw: float
    workouts_per_week: float
    pace_claims: dict[str, PaceClaim | None]
    peak_week: float | None
    peak_3wk_avg: float | None
    source: Literal["detected", "window"]


class BuildDay(TypedDict):
    date: str
    name: str | None
    distance_mi: float | None
    moving_time_s: int | None
    pace_min_per_mile: float | None
    avg_hr: int | None
    run_type: str
    workout_label: str | None
    id: int


class BuildDetailWeek(TypedDict):
    monday: str
    offset: int
    miles: float
    runs: int


class BuildDetail(TypedDict):
    race: BuildDetailRace
    build: BuildDetailStat
    days: list[BuildDay]
    weeks: list[BuildDetailWeek]


def _race_row_and_build(conn: sqlite3.Connection, race_date: str) -> tuple[dict[str, object], Build | None]:
    """
    Race row (from race_rows()) and its detected build (from detect_builds()),
    if any — the shared resolution build-detail and build-workout-groups both
    need to find a race's training window. 404s when no race matches the date;
    callers validate the date format themselves first (the 404 message differs).
    """
    race_row = next((r for r in race_rows(conn) if r["date"] == race_date), None)
    if race_row is None:
        raise HTTPException(status_code=404, detail=f"No race found on {race_date}.")

    weeks = _week_aggs(conn)
    periods, _gaps = detect_periods(weeks)
    race_activity_rows = _race_activity_rows(conn)
    builds = detect_builds(weeks, _race_refs(race_activity_rows), periods) if periods else []
    detected = next((b for b in builds if b["race"]["date"] == race_date), None)
    return race_row, detected


def _window_fallback(race_date: str, race_row: dict[str, object]) -> tuple[str, int]:
    """
    Fixed pre-race window (distance_builds.py's bucket convention) for races
    with no detected build. Returns (build_start, weeks).
    """
    bucket = _CATEGORY_TO_BUCKET.get(cast(str, race_row["distance_category"]), "Other")
    weeks_n = _DISTANCE_BUILD_WEEKS[bucket]
    build_start = _distance_build_start(race_date, weeks_n).isoformat()
    return build_start, weeks_n


@app.get("/api/build-detail")
def get_build_detail(race_date: str) -> BuildDetail:
    """
    Full drill-down for one race's training build, keyed by race date: the
    race result, the build's shape, every run in the build window (race day
    inclusive) for the calendar view, and per-week miles for the calendar's
    row labels.

    Prefers the race-anchored build from detect_builds() (same call
    weekly-history uses). Most races never anchor a detected build (anything
    below distance_builds.py's BUILD_ANCHOR_MIN_M, i.e. every 5K) — those fall
    back to the race's distance-bucket fixed Monday-aligned window
    (distance_builds.py's convention). `build.source` says which
    ("detected" | "window"). 404s only for a malformed date or no matching race.
    """
    try:
        race_dt = date.fromisoformat(race_date)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid race date {race_date!r}.")

    conn = _conn()
    race_row, detected = _race_row_and_build(conn, race_date)
    weeks = _week_aggs(conn)

    tc, tp = _type_clause()
    effective_run_type = db.effective_run_type_sql()

    source: Literal["detected", "window"]
    bounded_by: str | None
    peak_week: float | None = None
    if detected is not None:
        source = "detected"
        build_start = detected["start"]
        weeks_n = detected["weeks"]
        avg_mpw = detected["avg_mpw"]
        workouts_per_week = detected["workouts_per_week"]
        peak_3wk_avg = detected["peak_3wk_avg"]
        bounded_by = detected["bounded_by"]
    else:
        source = "window"
        bounded_by = None
        build_start, weeks_n = _window_fallback(race_date, race_row)

        # Same stats distance_builds.py computes for its own bucket tables
        # (avg over [build_start, race_date), calendar-week peak/rolling-3wk).
        totals = conn.execute(f"""
            SELECT ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ? AND DATE(start_date) < ?
        """, tp + [build_start, race_date]).fetchone()
        total_miles: float = (totals["total_miles"] if totals else None) or 0.0
        avg_mpw = round(total_miles / weeks_n, 1) if weeks_n else 0.0

        workout_totals = conn.execute(f"""
            SELECT SUM(CASE WHEN {effective_run_type} = 'workout' THEN 1 ELSE 0 END) AS workouts
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ? AND DATE(start_date) < ?
        """, tp + [build_start, race_date]).fetchone()
        total_workouts: int = (workout_totals["workouts"] if workout_totals else None) or 0
        workouts_per_week = round(total_workouts / weeks_n, 2) if weeks_n else 0.0

        peak = conn.execute(f"""
            WITH weekly AS (
                SELECT
                    strftime('%Y-W%W', start_date) AS week,
                    ROUND(SUM(distance_m) / 1609.34, 2) AS miles
                FROM activities
                WHERE {tc}
                  AND DATE(start_date) >= ? AND DATE(start_date) < ?
                GROUP BY week
            ),
            rolling AS (
                SELECT
                    miles,
                    ROUND(AVG(miles) OVER (
                        ORDER BY week ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
                    ), 1) AS r3
                FROM weekly
            )
            SELECT MAX(miles) AS peak_week, MAX(r3) AS peak_3wk_avg FROM rolling
        """, tp + [build_start, race_date]).fetchone()
        peak_week = peak["peak_week"] if peak else None
        peak_3wk_avg = peak["peak_3wk_avg"] if peak else None

    build_start_dt = date.fromisoformat(build_start)
    race_monday = race_dt - timedelta(days=race_dt.weekday())

    mondays: list[date] = []
    d = build_start_dt
    while d <= race_monday:
        mondays.append(d)
        d += timedelta(weeks=1)
    n = len(mondays)

    by_monday = {w["monday"]: w for w in weeks}
    week_out = [
        BuildDetailWeek(
            monday=m.isoformat(),
            offset=i - (n - 1),
            miles=round(by_monday[m.isoformat()]["miles"], 1) if m.isoformat() in by_monday else 0.0,
            runs=by_monday[m.isoformat()]["runs"] if m.isoformat() in by_monday else 0,
        )
        for i, m in enumerate(mondays)
    ]
    if source == "detected":
        # Unlike the window path's exclusive-of-race_date SQL peak, this reuses
        # the calendar's own Monday-zero-filled weeks — matches the original
        # detected-build behavior exactly.
        peak_week = max((w["miles"] for w in week_out), default=None)

    day_rows = conn.execute(f"""
        SELECT
            activity_id AS id,
            name,
            DATE(start_date) AS date,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            ROUND(average_heartrate) AS avg_hr,
            {effective_run_type} AS run_type,
            workout_label
        FROM activities
        WHERE {tc}
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        ORDER BY start_date
    """, tp + [build_start, race_date]).fetchall()

    days = [
        BuildDay(
            date=r["date"],
            name=r["name"],
            distance_mi=r["distance_mi"],
            moving_time_s=r["moving_time_s"],
            pace_min_per_mile=r["pace_min_per_mile"],
            avg_hr=int(r["avg_hr"]) if r["avg_hr"] is not None else None,
            run_type=r["run_type"],
            workout_label=r["workout_label"],
            id=r["id"],
        )
        for r in day_rows
    ]

    return BuildDetail(
        race=BuildDetailRace(
            name=cast("str | None", race_row["name"]),
            date=cast(str, race_row["date"]),
            distance_category=cast(str, race_row["distance_category"]),
            finish_time=cast(str, race_row["finish_time"]),
            finish_time_s=cast("int | None", race_row["finish_time_s"]),
            pace_min_per_mile=cast("float | None", race_row["pace_min_per_mile"]),
            effort=cast("str | None", race_row["effort"]),
            id=cast(int, race_row["activity_id"]),
        ),
        build=BuildDetailStat(
            start=build_start,
            weeks=weeks_n,
            bounded_by=bounded_by,
            avg_mpw=avg_mpw,
            workouts_per_week=workouts_per_week,
            pace_claims=pace_claims(conn, build_start, race_date),
            peak_week=peak_week,
            peak_3wk_avg=peak_3wk_avg,
            source=source,
        ),
        days=days,
        weeks=week_out,
    )


class ActivityLap(TypedDict):
    lap_index: int
    distance_mi: float | None
    moving_time_s: int | None
    pace_min_per_mile: float | None
    avg_hr: int | None
    lap_type: str | None


@app.get("/api/activity-laps")
def get_activity_laps(id: int) -> list[ActivityLap]:
    """
    Laps for one activity, ordered by lap index — the source for build.html's
    click-to-expand lap table. Empty list when no laps are synced for it.
    404s only when the activity id itself doesn't exist.
    """
    conn = _conn()
    exists = conn.execute("SELECT 1 FROM activities WHERE activity_id = ?", [id]).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"No activity {id}.")

    rows = conn.execute("""
        SELECT
            lap_index,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            ROUND(average_heartrate) AS avg_hr,
            lap_type
        FROM laps
        WHERE activity_id = ?
        ORDER BY lap_index
    """, [id]).fetchall()

    return [
        ActivityLap(
            lap_index=r["lap_index"],
            distance_mi=r["distance_mi"],
            moving_time_s=r["moving_time_s"],
            pace_min_per_mile=r["pace_min_per_mile"],
            avg_hr=int(r["avg_hr"]) if r["avg_hr"] is not None else None,
            lap_type=r["lap_type"],
        )
        for r in rows
    ]


class WorkAgg(TypedDict):
    laps: int
    distance_mi: float
    pace_min_per_mile: float | None
    avg_hr: int | None


class WorkoutGroupSession(TypedDict):
    id: int
    date: str
    name: str | None
    workout_label: str | None
    distance_mi: float | None
    pace_min_per_mile: float | None
    avg_hr: int | None
    temp_f: int | None
    work: WorkAgg | None


class WorkoutGroup(TypedDict):
    label: str
    sessions: list[WorkoutGroupSession]


# Artifact-lap floor shared with compare_workouts_by_build's work-lap stats
# (mcp_server.py) — trivial laps (button mashes, GPS blips) never count as work.
_WORK_LAP_MIN_TIME_S = 45
_WORK_LAP_MIN_DIST_M = 200


@app.get("/api/build-workout-groups")
def get_build_workout_groups(race_date: str) -> list[WorkoutGroup]:
    """
    Repeated-session comparison for one race's build window — same window
    resolution as /api/build-detail (detected build, or fixed distance-bucket
    window as fallback). Groups workout-type activities by workout_label (only
    labels with 2+ sessions get their own group; label-less or singleton-label
    sessions fall into "Other workouts"), plus a "Long runs" group. Each
    session's `work` aggregates its lap_type='work' laps (distance-weighted
    pace, artifact-filtered); null when the activity has no qualifying work
    laps. Long-run sessions always have work=null — their own totals suffice.
    `temp_f` is the session's average temperature (weather.temp_c_avg,
    converted to Fahrenheit and rounded); null when no weather row was synced
    for the activity. Labeled groups sorted by session count descending, then
    Long runs, then Other workouts; sessions within a group are date-ascending.
    Empty groups are omitted. 404s only for a malformed date or no matching race.
    """
    try:
        date.fromisoformat(race_date)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid race date {race_date!r}.")

    conn = _conn()
    race_row, detected = _race_row_and_build(conn, race_date)
    build_start = detected["start"] if detected is not None else _window_fallback(race_date, race_row)[0]

    tc, tp = _type_clause()
    effective_run_type = db.effective_run_type_sql()
    day_rows = conn.execute(f"""
        SELECT
            activities.activity_id AS id,
            DATE(start_date) AS date,
            name,
            workout_label,
            {effective_run_type} AS run_type,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            ROUND(average_heartrate) AS avg_hr,
            ROUND(weather.temp_c_avg * 9.0 / 5 + 32) AS temp_f
        FROM activities
        LEFT JOIN weather ON weather.activity_id = activities.activity_id
        WHERE {tc}
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
          AND {effective_run_type} IN ('workout', 'long_run')
        ORDER BY start_date
    """, tp + [build_start, race_date]).fetchall()

    ids = [int(r["id"]) for r in day_rows]
    work_by_id: dict[int, WorkAgg] = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        work_rows = conn.execute(f"""
            SELECT
                activity_id,
                COUNT(*) AS n_laps,
                SUM(distance_m) AS dist_m,
                SUM(moving_time_s) AS time_s,
                AVG(average_heartrate) AS avg_hr
            FROM laps
            WHERE activity_id IN ({placeholders})
              AND lap_type = 'work'
              AND moving_time_s >= {_WORK_LAP_MIN_TIME_S} AND distance_m >= {_WORK_LAP_MIN_DIST_M}
            GROUP BY activity_id
        """, ids).fetchall()
        for r in work_rows:
            dist_m: float = r["dist_m"] or 0.0
            time_s: float = r["time_s"] or 0.0
            work_by_id[int(r["activity_id"])] = WorkAgg(
                laps=r["n_laps"],
                distance_mi=round(dist_m / 1609.34, 2),
                # Distance-weighted pace == total time / total distance (the
                # per-lap-pace-weighted-by-distance average reduces to this).
                pace_min_per_mile=round((time_s / 60) / (dist_m / 1609.34), 2) if dist_m > 0 else None,
                avg_hr=round(r["avg_hr"]) if r["avg_hr"] is not None else None,
            )

    def _session(r: sqlite3.Row) -> WorkoutGroupSession:
        aid = int(r["id"])
        return WorkoutGroupSession(
            id=aid,
            date=r["date"],
            name=r["name"],
            workout_label=r["workout_label"],
            distance_mi=r["distance_mi"],
            pace_min_per_mile=r["pace_min_per_mile"],
            avg_hr=int(r["avg_hr"]) if r["avg_hr"] is not None else None,
            temp_f=int(r["temp_f"]) if r["temp_f"] is not None else None,
            work=work_by_id.get(aid) if r["run_type"] == "workout" else None,
        )

    long_runs = [_session(r) for r in day_rows if r["run_type"] == "long_run"]

    by_label: dict[str, list[WorkoutGroupSession]] = {}
    other: list[WorkoutGroupSession] = []
    for r in day_rows:
        if r["run_type"] != "workout":
            continue
        label = r["workout_label"]
        if label:
            by_label.setdefault(label, []).append(_session(r))
        else:
            other.append(_session(r))

    labeled: list[tuple[str, list[WorkoutGroupSession]]] = []
    for label, sessions in by_label.items():
        if len(sessions) >= 2:
            labeled.append((label, sessions))
        else:
            other.extend(sessions)
    labeled.sort(key=lambda ls: len(ls[1]), reverse=True)

    groups: list[WorkoutGroup] = [WorkoutGroup(label=label, sessions=sessions) for label, sessions in labeled]
    if long_runs:
        groups.append(WorkoutGroup(label="Long runs", sessions=long_runs))
    if other:
        other.sort(key=lambda s: s["date"])
        groups.append(WorkoutGroup(label="Other workouts", sessions=other))

    return groups


# source_tier -> short label for what backed the estimate (see fitness.py:
# tier-1 recency-weighted races, tier-2 workout work-laps, tier-3 training-
# pace envelope floor).
_FITNESS_BASIS_BY_TIER: dict[int, str] = {
    1: "race",
    2: "workout anchor",
    3: "training floor",
}


class FitnessTrendPoint(TypedDict):
    date: str
    fivek_pace_min_per_mile: float
    confidence: str
    basis: str | None


@app.get("/api/fitness-trend")
def get_fitness_trend() -> list[FitnessTrendPoint]:
    """
    Monthly fitness checkpoints, oldest first — a cheap read of the derived
    fitness_checkpoints table (rebuilt each sync; never recomputed here).
    pace_5k is already a decimal min/mi pace, not a time. date is the first of
    the checkpoint's month (the table only stores 'YYYY-MM').
    """
    conn = _conn()
    rows = conn.execute("""
        SELECT month, confidence, source_tier, pace_5k
        FROM fitness_checkpoints
        WHERE pace_5k IS NOT NULL
        ORDER BY month
    """).fetchall()

    return [
        FitnessTrendPoint(
            date=f"{r['month']}-01",
            fivek_pace_min_per_mile=round(float(r["pace_5k"]), 2),
            confidence=cast(str, r["confidence"]),
            basis=_FITNESS_BASIS_BY_TIER.get(r["source_tier"]),
        )
        for r in rows
    ]


class RaceRow(TypedDict):
    date: str
    name: str | None
    distance_category: str
    distance_miles: float | None
    finish_time_s: int | None
    finish_time: str
    pace_min_per_mile: str | None
    is_pr: bool
    effort: str | None
    activity_id: int
    strava_url: str | None


@app.get("/api/races")
def get_races() -> list[RaceRow]:
    """
    Every effective race at every distance, ascending by date, with
    per-category PR flags computed over the full history.
    """
    conn = _conn()
    out: list[RaceRow] = []
    for r in race_rows(conn):
        pace = r["pace_min_per_mile"]
        out.append(RaceRow(
            date=cast(str, r["date"]),
            name=cast(str | None, r["name"]),
            distance_category=cast(str, r["distance_category"]),
            distance_miles=cast(float | None, r["distance_miles"]),
            finish_time_s=cast(int | None, r["finish_time_s"]),
            finish_time=cast(str, r["finish_time"]),
            pace_min_per_mile=fmt_pace(pace) if isinstance(pace, (int, float)) else None,
            is_pr=cast(bool, r["is_pr"]),
            effort=cast(str | None, r["effort"]),
            activity_id=cast(int, r["activity_id"]),
            strava_url=cast(str | None, r["strava_url"]),
        ))
    return out


class YearWeekPoint(TypedDict):
    week: int
    miles: float


class LongestRun(TypedDict):
    date: str
    miles: float


class YearRace(TypedDict):
    date: str
    name: str | None
    distance_category: str
    finish_time: str
    is_pr: bool
    effort: str | None


class YearHighlights(TypedDict):
    total_miles: float
    runs: int
    active_weeks: int
    longest_run: LongestRun | None
    peak_week_miles: float
    races: list[YearRace]
    prs_set: int


class YearRow(TypedDict):
    year: int
    weeks: list[YearWeekPoint]
    highlights: YearHighlights


@app.get("/api/years")
def get_years() -> list[YearRow]:
    """
    Per-calendar-year weekly mileage (Monday-aligned; week 1 starts at the
    year's first Monday, days before it belong to the previous year's final
    week) plus highlights: totals, active weeks, longest run, peak week,
    races, PRs set. Current year's series stops at the current week; future
    weeks are absent, not zero. Years with no runs are omitted. Highlights
    totals are calendar-year, so year-boundary weeks can differ slightly
    from the chart's weekly sum.
    """
    conn = _conn()
    tc, tp = _type_clause()
    today = date.today()
    current_year = today.year
    current_monday = today - timedelta(days=today.weekday())

    totals = conn.execute(f"""
        SELECT
            CAST(strftime('%Y', start_date) AS INTEGER) AS year,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs
        FROM activities
        WHERE {tc}
        GROUP BY year
    """, tp).fetchall()
    year_total: dict[int, float] = {r["year"]: r["miles"] or 0.0 for r in totals}
    year_runs: dict[int, int] = {r["year"]: r["runs"] for r in totals}

    run_rows = conn.execute(f"""
        SELECT
            CAST(strftime('%Y', start_date) AS INTEGER) AS year,
            DATE(start_date) AS d,
            ROUND(distance_m / 1609.34, 2) AS miles
        FROM activities
        WHERE {tc}
    """, tp).fetchall()

    longest: dict[int, LongestRun] = {}
    for r in run_rows:
        year, miles = r["year"], r["miles"] or 0.0
        best = longest.get(year)
        if best is None or miles > best["miles"]:
            longest[year] = LongestRun(date=r["d"], miles=miles)

    # Monday-aligned weeks across all history, bucketed by the calendar year
    # the Monday falls in — the source for the weekly series, peak week, and
    # active-week counts alike.
    monday_rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs
        FROM activities
        WHERE {tc}
        GROUP BY monday
    """, tp).fetchall()

    def _week_no(monday: date) -> int:
        jan1 = date(monday.year, 1, 1)
        first_monday = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
        return (monday - first_monday).days // 7 + 1

    week_miles: dict[int, dict[int, float]] = {}
    for r in monday_rows:
        m = date.fromisoformat(r["monday"])
        week_miles.setdefault(m.year, {})[_week_no(m)] = r["miles"] or 0.0

    active_weeks: dict[int, int] = {}
    for r in monday_rows:
        wk: WeekAgg = {"monday": r["monday"], "miles": r["miles"] or 0.0, "runs": r["runs"], "workouts": 0}
        if is_active(wk):
            yr = date.fromisoformat(r["monday"]).year
            active_weeks[yr] = active_weeks.get(yr, 0) + 1

    races_by_year: dict[int, list[YearRace]] = {}
    prs_by_year: dict[int, int] = {}
    for r in race_rows(conn):
        yr = date.fromisoformat(cast(str, r["date"])).year
        races_by_year.setdefault(yr, []).append(YearRace(
            date=cast(str, r["date"]),
            name=cast(str | None, r["name"]),
            distance_category=cast(str, r["distance_category"]),
            finish_time=cast(str, r["finish_time"]),
            is_pr=cast(bool, r["is_pr"]),
            effort=cast(str | None, r["effort"]),
        ))
        if r["is_pr"]:
            prs_by_year[yr] = prs_by_year.get(yr, 0) + 1

    out: list[YearRow] = []
    for year in sorted(set(week_miles) | set(year_total)):
        # Zero-mile years are logging artifacts (e.g. a 0-distance HR reading
        # recorded as a run), not running years.
        if year_total.get(year, 0.0) <= 0:
            continue
        if year == current_year:
            last_monday = current_monday
        else:
            last_monday = date(year, 12, 31)
            last_monday -= timedelta(days=last_monday.weekday())
        last_week = max(1, _week_no(last_monday))
        wm = week_miles.get(year, {})
        weeks = [YearWeekPoint(week=w, miles=wm.get(w, 0.0)) for w in range(1, last_week + 1)]
        out.append(YearRow(
            year=year,
            weeks=weeks,
            highlights=YearHighlights(
                total_miles=year_total.get(year, 0.0),
                runs=year_runs.get(year, 0),
                active_weeks=active_weeks.get(year, 0),
                longest_run=longest.get(year),
                peak_week_miles=max(wm.values(), default=0.0),
                races=sorted(races_by_year.get(year, []), key=lambda r: r["date"]),
                prs_set=prs_by_year.get(year, 0),
            ),
        ))
    return out


_HR_BUCKET_WIDTH = 5
_PACE_BUCKET_WIDTH_S = 20


class HrPacePoint(TypedDict):
    year: int
    hr_bucket: int  # bpm, bucket lower bound
    pace_bucket: int  # seconds/mile, bucket lower bound


@app.get("/api/hr-pace-heatmap")
def get_hr_pace_heatmap() -> list[HrPacePoint]:
    """
    One point per lap (avg HR, avg pace), bucketed into a 5bpm x 20s/mi grid.
    Excludes trivial laps (< 200m or < 45s) and laps missing HR. Returns raw
    points, not pre-aggregated cells, so the client can combine any set of
    selected years.
    """
    conn = _conn()
    tc, tp = _type_clause()
    rows = conn.execute(f"""
        SELECT
            CAST(strftime('%Y', a.start_date) AS INTEGER) AS year,
            l.average_heartrate AS hr,
            l.distance_m AS distance_m,
            l.moving_time_s AS moving_time_s
        FROM laps l
        JOIN activities a ON a.activity_id = l.activity_id
        WHERE {tc}
          AND l.average_heartrate IS NOT NULL
          AND l.distance_m >= {LAP_MIN_DISTANCE_M}
          AND l.moving_time_s >= {LAP_MIN_MOVING_TIME_S}
    """, tp).fetchall()

    out: list[HrPacePoint] = []
    for r in rows:
        miles = r["distance_m"] / 1609.34
        pace_s_per_mile = r["moving_time_s"] / miles
        hr_bucket = int(r["hr"] // _HR_BUCKET_WIDTH) * _HR_BUCKET_WIDTH
        pace_bucket = int(pace_s_per_mile // _PACE_BUCKET_WIDTH_S) * _PACE_BUCKET_WIDTH_S
        out.append(HrPacePoint(year=r["year"], hr_bucket=hr_bucket, pace_bucket=pace_bucket))
    return out


@app.get("/api/workbooks")
def list_workbooks() -> list[str]:
    return sorted(p.name for p in _WORKBOOKS.glob("*.html"))


@app.post("/api/workbooks/upload")
async def upload_workbook(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename or not file.filename.endswith(".html"):
        raise HTTPException(status_code=400, detail="Only .html files are accepted")
    dest = _WORKBOOKS / Path(file.filename).name
    content = await file.read()
    dest.write_bytes(content)
    return JSONResponse({"name": dest.name})


class SyncTriggerResponse(TypedDict):
    status: str


class SyncStatusResponse(TypedDict):
    status: str
    returncode: int | None


@app.post("/api/sync")
def trigger_sync() -> SyncTriggerResponse:
    global _sync_proc
    if _sync_proc is not None and _sync_proc.poll() is None:
        return SyncTriggerResponse(status="running")
    try:
        _sync_proc = _subprocess.Popen(
            ["uv", "run", "miles-sync", "--extra"],
            cwd=_REPO_ROOT,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.PIPE,
        )
    except FileNotFoundError:
        _logger.error("uv not found on PATH; cannot start miles-sync")
        raise HTTPException(status_code=500, detail="sync command not found")
    return SyncTriggerResponse(status="started")


@app.get("/api/sync/status")
def sync_status() -> SyncStatusResponse:
    if _sync_proc is None:
        return SyncStatusResponse(status="idle", returncode=None)
    rc = _sync_proc.poll()
    if rc is None:
        return SyncStatusResponse(status="running", returncode=None)
    if rc != 0 and _sync_proc.stderr is not None:
        err = _sync_proc.stderr.read().decode(errors="replace")
        _logger.warning("miles-sync exited with code %d: %s", rc, err)
    return SyncStatusResponse(status="done", returncode=rc)


# Must precede the catch-all static mount below.
app.include_router(distance_builds_router)
app.include_router(fitness_api_router)

app.mount("/workbooks", StaticFiles(directory=str(_WORKBOOKS)), name="workbooks")
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")


@click.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=8000, type=int, help="Port to bind to.")
@click.option("--reload/--no-reload", default=True, help="Enable auto-reload on code changes.")
def main(host: str, port: int, reload: bool) -> None:
    uvicorn.run("miles.api:app", host=host, port=port, reload=reload)
