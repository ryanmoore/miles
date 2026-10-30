import html
import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

import anyio.to_thread
import click
import pandas as pd
import uvicorn
from typing_extensions import NotRequired, TypedDict

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from . import areas, db, heat, outbound, plan, route_search, routes, strava_budget, strava_client, streams, sync, terrain
from .build_paces import PaceClaim, pace_claims
from .fitness import FT_PER_M
from .classifier import LAP_MIN_DISTANCE_M, LAP_MIN_MOVING_TIME_S, WORKOUT_LABEL_PATTERNS, classify_workout
from .build_stats import BuildStat, WeekPoint, build_stat, peak_weeks, week_offset_points, window_total_miles
from .distance_builds import (
    Bucket,
    router as distance_builds_router,
    _BUILD_WEEKS as _DISTANCE_BUILD_WEEKS,
    _build_start as _distance_build_start,
    _DISTANCE_CATEGORY,
)
from .areas_api import router as areas_api_router
from .candidates_api import router as candidates_api_router
from .fitness_api import router as fitness_api_router
from .easy_efficiency import router as easy_efficiency_router
from .health_api import router as health_api_router
from .load_api import router as load_api_router
from .plan_api import router as plan_api_router
from .routes_api import router as routes_api_router
from .searches_api import router as searches_api_router
from .terrain_api import router as terrain_api_router
from .builds import Build, detect_builds
from .derive import connect_derived as _conn, derive_all
from .errors import StravaAllowanceLowError, StravaNotConfiguredError, describe_error
from .format import fmt_pace, fmt_time
from .periods import Gap, Period, WeekAgg, is_active, monday_of, zero_fill, detect_periods
from .mcp_server import mcp as _mcp_server
from .weather import ATTRIBUTION as WEATHER_ATTRIBUTION
from .races import (
    classify_race_distance,
    effective_race_activities,
    full_periods_and_builds,
    marathon_races,
    race_refs,
    race_rows,
    run_sport_filter,
    week_aggs,
)

# The MCP server, served over streamable HTTP by this same process at /mcp,
# so a remote Claude Code session can reach the tools (and this instance's
# DB) with `claude mcp add --transport http`. `miles-mcp` remains the stdio
# entrypoint for local use. streamable_http_app() must be built before the
# session manager exists; the transport rejects requests unless that manager
# is running, hence the lifespan below.
_mcp_app = _mcp_server.streamable_http_app()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    outbound.claim()
    terrain.start_background_rebuild_if_stale()
    async with _mcp_server.session_manager.run():
        yield


app = FastAPI(title="miles", lifespan=_lifespan)


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


for _not_found_error in (areas.AreaNotFoundError, route_search.CandidateNotFoundError, routes.RouteNotFoundError):
    app.add_exception_handler(_not_found_error, _not_found)


@app.exception_handler(terrain.TerrainRebuildingError)
async def _terrain_rebuilding(request: Request, exc: terrain.TerrainRebuildingError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Retry-After": "60"})

@app.exception_handler(StravaAllowanceLowError)
async def _strava_allowance_low(request: Request, exc: StravaAllowanceLowError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Retry-After": "900"})


@app.exception_handler(StravaNotConfiguredError)
async def _strava_not_configured(request: Request, exc: StravaNotConfiguredError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


_logger = logging.getLogger(__name__)
_sync_progress = sync.SyncProgress()

# GIT_HASH/BUILD_TIME are baked into the image env at `docker build` time
# (see Dockerfile, docker-build.sh). Outside Docker, or if unset, fall back
# to "latest" and this process's actual start time.
_PROCESS_START_TIME = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VersionInfo(TypedDict):
    git_hash: str
    build_time: str


class ClientConfig(TypedDict):
    tile_url: str | None
    tile_attribution: str | None
    tile_max_zoom: int | None


_DEFAULT_TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
_warned_tile_attribution: list[bool] = []


def _tile_max_zoom() -> int | None:
    try:
        return int(os.environ.get("MILES_TILE_MAX_ZOOM", "")) or None
    except ValueError:
        return None


@app.get("/api/config")
def get_client_config() -> ClientConfig:
    """Deployment settings the static pages need: an optional base-layer
    override (MILES_TILE_URL, with MILES_TILE_ATTRIBUTION and
    MILES_TILE_MAX_ZOOM). Unset or empty means the pages use their defaults.
    A custom tile layer without an attribution is credited to OpenStreetMap,
    whose data any tile server built on it carries."""
    tile_url = os.environ.get("MILES_TILE_URL") or None
    attribution = os.environ.get("MILES_TILE_ATTRIBUTION") or None
    if tile_url and not attribution:
        if not _warned_tile_attribution:
            _warned_tile_attribution.append(True)
            _logger.warning(
                "MILES_TILE_URL is set without MILES_TILE_ATTRIBUTION; crediting OpenStreetMap contributors. "
                "Set MILES_TILE_ATTRIBUTION to the credit your tile provider requires."
            )
        attribution = _DEFAULT_TILE_ATTRIBUTION
    return {
        "tile_url": tile_url,
        "tile_attribution": attribution,
        "tile_max_zoom": _tile_max_zoom(),
    }


@app.get("/api/version")
def get_version() -> VersionInfo:
    return {
        "git_hash": os.environ.get("GIT_HASH") or "latest",
        "build_time": os.environ.get("BUILD_TIME") or _PROCESS_START_TIME,
    }

_BUILD_WEEKS = 12
_STATIC = Path(__file__).parent / "static"
_WORKBOOKS = Path("data/workbooks")
_WORKBOOKS.mkdir(parents=True, exist_ok=True)

# Inverse of distance_builds.py's Bucket -> category map, so build-detail can
# find a race's fixed-window bucket from race_rows()'s distance_category.
# Categories with no dedicated bucket (15K/10M/30K) fall back to "Other".
_CATEGORY_TO_BUCKET: dict[str, Bucket] = {cat: bucket for bucket, cat in _DISTANCE_CATEGORY.items()}


class MarathonRow(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    distance_miles: float | None
    pace_min_per_mile: float | None
    build: BuildStat


def _fetch_interactive_laps(conn: sqlite3.Connection, activity_id: int) -> None:
    """Fetch and store one activity's laps for a page that needs them. At the
    Strava allowance's reserve nothing is spent and the laps stay unfetched."""
    with suppress(StravaAllowanceLowError):
        laps = strava_budget.Gate().call("laps", lambda: strava_client.get_activity_laps(activity_id), interactive=True)
        db.store_activity_laps(conn, activity_id, laps)


@app.get("/api/marathons")
def get_marathons(build_weeks: int = _BUILD_WEEKS) -> list[MarathonRow]:
    """
    All marathon race results with stats for the build_weeks-week training
    block that preceded each one. Sorted by date ascending.
    """
    conn = _conn()

    out: list[MarathonRow] = []
    for race in marathon_races(conn):
        race_date: str = race["race_date"]
        out.append(MarathonRow(
            name=race["name"],
            date=race_date,
            finish_time_s=race["moving_time_s"],
            finish_time=fmt_time(race["moving_time_s"]),
            distance_miles=race["distance_miles"],
            pace_min_per_mile=race["pace_min_per_mile"],
            build=build_stat(conn, _distance_build_start(race_date, build_weeks).isoformat(), race_date, build_weeks),
        ))

    return out


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

    out: list[MarathonWeeks] = []
    for race in marathon_races(conn):
        race_date: str = race["race_date"]
        out.append(MarathonWeeks(
            name=race["name"],
            date=race_date,
            finish_time_s=race["moving_time_s"],
            finish_time=fmt_time(race["moving_time_s"]),
            weeks=week_offset_points(conn, _distance_build_start(race_date, build_weeks).isoformat(), race_date, build_weeks),
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
    finish_time_s: int | None
    effort: str | None


class WeeklyHistory(TypedDict):
    weeks: list[HistoryWeek]
    periods: list[Period]
    gaps: list[Gap]
    builds: list[Build]
    races: list[RaceMarker]
    race_plans: dict[str, int]  # race date -> completed plan_id


@app.get("/api/weekly-history")
def get_weekly_history() -> WeeklyHistory:
    """
    Every week of running history, zero-filled, plus detected training periods,
    gaps, race-anchored builds within them, and every race with its effort label
    and finish time. Build.race carries no result fields; join races to builds
    by date. race_plans maps a completed plan's id by its matching race's exact
    date (plan.completed_plans_by_race_date), the same key as Build.race.date.
    """
    conn = _conn()

    weeks = week_aggs(conn)
    filled_weeks = zero_fill(weeks)
    periods, gaps = detect_periods(weeks)

    race_activity_rows = effective_race_activities(conn)
    races = [
        RaceMarker(
            date=r["date"],
            name=r["name"],
            distance_category=classify_race_distance(r["distance_m"]) or "other",
            finish_time_s=r["moving_time_s"],
            effort=r["race_effort"],
        )
        for r in race_activity_rows
    ]
    builds = detect_builds(weeks, race_refs(race_activity_rows), periods) if periods else []
    race_plans = {date: row["plan_id"] for date, row in plan.completed_plans_by_race_date(conn).items()}

    return WeeklyHistory(
        weeks=[HistoryWeek(monday=w["monday"], miles=w["miles"], runs=w["runs"]) for w in filled_weeks],
        periods=periods,
        gaps=gaps,
        builds=builds,
        races=races,
        race_plans=race_plans,
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


def _race_row_and_build(
    conn: sqlite3.Connection, race_date: str
) -> tuple[dict[str, object], Build | None, list[WeekAgg]]:
    """
    Race row (from race_rows()), its detected build (from detect_builds()),
    if any, and the weekly aggregates over the whole history. 404s when no race matches the date; callers validate the date
    format themselves first.
    """
    race_row = next((r for r in race_rows(conn) if r["date"] == race_date), None)
    if race_row is None:
        raise HTTPException(status_code=404, detail=f"No race found on {race_date}.")

    _periods, builds, weeks = full_periods_and_builds(conn)
    detected = next((b for b in builds if b["race"]["date"] == race_date), None)
    return race_row, detected, weeks


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

    Prefers the race-anchored build from detect_builds(). Races below
    BUILD_ANCHOR_MIN_M anchor none and fall back to the race's distance-bucket
    fixed Monday-aligned window (distance_builds.py); `build.source` says
    which ("detected" | "window"). 404s only for a malformed date or no
    matching race.
    """
    try:
        race_dt = date.fromisoformat(race_date)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Invalid race date {race_date!r}.")

    conn = _conn()
    race_row, detected, weeks = _race_row_and_build(conn, race_date)

    tc, tp = run_sport_filter()
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

        avg_mpw = round(window_total_miles(conn, build_start, race_date) / weeks_n, 1) if weeks_n else 0.0

        workout_totals = conn.execute(f"""
            SELECT SUM(CASE WHEN {effective_run_type} = 'workout' THEN 1 ELSE 0 END) AS workouts
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ? AND DATE(start_date) < ?
        """, tp + [build_start, race_date]).fetchone()
        total_workouts: int = (workout_totals["workouts"] if workout_totals else None) or 0
        workouts_per_week = round(total_workouts / weeks_n, 2) if weeks_n else 0.0

        peak_week, peak_3wk_avg = peak_weeks(conn, build_start, race_date)

    build_start_dt = date.fromisoformat(build_start)
    race_monday = monday_of(race_dt)

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
            {db.pace_sql("average_speed_mps")} AS pace_min_per_mile,
            ROUND({db.hr_sql("average_heartrate")}) AS avg_hr,
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


class ActivityListRow(TypedDict):
    id: int
    name: str | None
    start_date: str | None
    sport_type: str | None
    is_run: bool
    run_type: str | None  # null for non-runs
    distance_mi: float | None
    moving_time_s: int | None
    pace_min_per_mile: float | None
    avg_hr: int | None


class ActivityListPage(TypedDict):
    rows: list[ActivityListRow]
    has_more: bool


class SportCount(TypedDict):
    sport_type: str
    count: int


class RunTypeCount(TypedDict):
    run_type: str
    count: int


class ActivityFilters(TypedDict):
    sports: list[SportCount]  # most activities first
    run_types: list[RunTypeCount]  # effective run types of sport_type 'Run'
    first_month: str | None  # YYYY-MM
    last_month: str | None


@app.get("/api/activity-filters")
def get_activity_filters() -> ActivityFilters:
    """Options for the activities page filters: the sport types and Run's
    effective run types present in the DB, and the span of months covered."""
    conn = _conn()
    sports = conn.execute("""
        SELECT sport_type, COUNT(*) AS n FROM activities
        WHERE sport_type IS NOT NULL
        GROUP BY sport_type ORDER BY n DESC, sport_type
    """).fetchall()
    run_types = conn.execute(f"""
        SELECT {db.EFFECTIVE_RUN_TYPE_SQL} AS t, COUNT(*) AS n FROM activities
        WHERE sport_type = 'Run' AND {db.EFFECTIVE_RUN_TYPE_SQL} IS NOT NULL
        GROUP BY t ORDER BY n DESC
    """).fetchall()
    span = conn.execute("SELECT MIN(start_date) AS lo, MAX(start_date) AS hi FROM activities").fetchone()
    return ActivityFilters(
        sports=[SportCount(sport_type=r["sport_type"], count=r["n"]) for r in sports],
        run_types=[RunTypeCount(run_type=r["t"], count=r["n"]) for r in run_types],
        first_month=span["lo"][:7] if span["lo"] else None,
        last_month=span["hi"][:7] if span["hi"] else None,
    )


def _month_start(month: str) -> date:
    year, mon = month.split("-")
    return date(int(year), int(mon), 1)


def _activity_filter_sql(
    sport: list[str], run_type: list[str], from_month: str | None, to_month: str | None,
) -> tuple[str, list[object]]:
    """WHERE clause for the activities list. `run_type` narrows only
    sport_type 'Run' rows; empty lists mean no filter."""
    conds: list[str] = []
    params: list[object] = []
    run_cond = f"{db.EFFECTIVE_RUN_TYPE_SQL} IN ({','.join('?' * len(run_type))})"
    if sport:
        others = [s for s in sport if s != "Run"]
        alts: list[str] = []
        if others:
            alts.append(f"sport_type IN ({','.join('?' * len(others))})")
            params.extend(others)
        if "Run" in sport:
            alts.append(f"(sport_type = 'Run' AND {run_cond})" if run_type else "sport_type = 'Run'")
            params.extend(run_type)
        conds.append(f"({' OR '.join(alts)})")
    elif run_type:
        conds.append(f"(sport_type IS NOT 'Run' OR {run_cond})")
        params.extend(run_type)
    if from_month:
        conds.append("DATE(start_date) >= ?")
        params.append(_month_start(from_month).isoformat())
    if to_month:
        end = _month_start(to_month)
        next_month = date(end.year + end.month // 12, end.month % 12 + 1, 1)
        conds.append("DATE(start_date) < ?")
        params.append(next_month.isoformat())
    return (" AND ".join(conds) or "1"), params


_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


@app.get("/api/activities-list")
def get_activities_list(
    offset: int = 0,
    limit: int = 50,
    sport: list[str] = Query(default=[]),
    run_type: list[str] = Query(default=[]),
    from_month: str | None = Query(default=None, pattern=_MONTH_PATTERN),
    to_month: str | None = Query(default=None, pattern=_MONTH_PATTERN),
) -> ActivityListPage:
    """
    Paginated activity index, newest first, every sport. Non-runs carry no
    run_type or pace, and distance only when they have one. limit is capped
    at 200. Filters: `sport` (repeatable sport_type), `run_type` (repeatable;
    narrows only 'Run' rows), `from_month`/`to_month` (YYYY-MM, inclusive).
    """
    limit = min(limit, 200)
    conn = _conn()
    sport_clause, sport_params = run_sport_filter()
    where, where_params = _activity_filter_sql(sport, run_type, from_month, to_month)
    rows = conn.execute(f"""
        SELECT
            activity_id,
            name,
            start_date,
            sport_type,
            {sport_clause} AS is_run,
            {db.EFFECTIVE_RUN_TYPE_SQL} AS run_type,
            ROUND(NULLIF(distance_m, 0) / 1609.34, 2) AS distance_mi,
            moving_time_s,
            {db.pace_sql("average_speed_mps")} AS pace_min_per_mile,
            ROUND(average_heartrate) AS avg_hr
        FROM activities
        WHERE {where}
        ORDER BY start_date DESC
        LIMIT ? OFFSET ?
    """, [*sport_params, *where_params, limit + 1, offset]).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    return ActivityListPage(
        rows=[
            ActivityListRow(
                id=r["activity_id"],
                name=r["name"],
                start_date=r["start_date"],
                sport_type=r["sport_type"],
                is_run=bool(r["is_run"]),
                run_type=r["run_type"] if r["is_run"] else None,
                distance_mi=r["distance_mi"],
                moving_time_s=r["moving_time_s"],
                pace_min_per_mile=r["pace_min_per_mile"] if r["is_run"] else None,
                avg_hr=int(r["avg_hr"]) if r["avg_hr"] is not None else None,
            )
            for r in rows
        ],
        has_more=has_more,
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
    Laps for one activity, ordered by lap index. Empty list when no laps are
    synced for it; 404s only when the activity id doesn't exist.
    """
    conn = _conn()
    exists = conn.execute("SELECT 1 FROM activities WHERE activity_id = ?", [id]).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"No activity {id}.")

    rows = conn.execute(f"""
        SELECT
            lap_index,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            moving_time_s,
            {db.pace_sql("average_speed_mps")} AS pace_min_per_mile,
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


class ActivityDetail(TypedDict):
    id: int
    name: str | None
    description: str | None
    start_date: str | None
    distance_mi: float | None
    moving_time_s: int | None
    elapsed_time_s: int | None
    pace_min_per_mile: float | None
    avg_hr: int | None
    min_hr: int | None
    median_hr: int | None
    max_hr: int | None
    elevation_gain_ft: float | None
    elevation_loss_ft: float | None
    temp_f_avg: float | None
    humidity_avg: float | None
    wind_mph_avg: float | None
    weather_attribution: str | None
    strava_url: str
    run_type: str
    race_effort: str | None
    workout_label: str | None
    hr_excluded: bool  # the athlete flagged this HR as wrong: shown here, left out of every chart
    polyline: str | None


@app.get("/api/activity-detail")
def get_activity_detail(id: int) -> ActivityDetail:
    """
    Header stats for activity.html. Description is lazily fetched from
    Strava's DetailedActivity (not present on the SummaryActivity rows sync
    stores) on first view of a given activity, then persisted so it's a
    one-time cost, not a per-page-view API call. Min/median/max HR and
    elevation loss come from the cached stream (fetched/cached lazily by
    /api/activity-streams — call that first; if it hasn't been called yet
    this falls back to activity-level average/max HR only, with min/median
    HR and elevation loss left null rather than triggering a second
    network fetch here).
    """
    conn = _conn()
    row = conn.execute(f"""
        SELECT
            activities.activity_id AS id, name, description, start_date,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            moving_time_s, elapsed_time_s,
            {db.pace_sql("average_speed_mps")} AS pace_min_per_mile,
            ROUND(average_heartrate) AS avg_hr,
            ROUND(max_heartrate) AS max_hr,
            ROUND(total_elevation_gain_m * 3.28084) AS elevation_gain_ft,
            strava_url,
            ROUND(weather.temp_c_avg * 9.0 / 5 + 32, 1) AS temp_f_avg,
            weather.humidity_avg,
            ROUND(weather.wind_kph_avg * 0.621371, 1) AS wind_mph_avg,
            run_type, race_effort, workout_label, hr_excluded_at, activities.raw_json AS raw_json
        FROM activities
        LEFT JOIN weather ON weather.activity_id = activities.activity_id
        WHERE activities.activity_id = ?
    """, [id]).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No activity {id}.")

    polyline: str | None = None
    if row["raw_json"] is not None:
        map_data = json.loads(row["raw_json"]).get("map")
        if map_data is not None:
            polyline = map_data.get("summary_polyline")

    description = row["description"]
    if description is None:
        try:
            description = strava_budget.Gate().call(
                "detail", lambda: strava_client.get_activity_detail(id)[0], interactive=True
            )
        except Exception:
            description = None
        else:
            db.set_activity_description(conn, id, description)

    min_hr: int | None = None
    median_hr: int | None = None
    elevation_loss_ft: float | None = None
    if streams.has_cached_streams(id):
        df = streams.fetch_and_cache_streams(id)
        hr = cast(pd.Series, df["heartrate"]).dropna()
        if len(hr):
            min_hr = int(cast(float, hr.min()))
            median_hr = int(cast(float, hr.median()))
        altitude = cast(pd.Series, df["altitude_m"]).dropna()
        if len(altitude) > 1:
            diffs = altitude.diff().dropna()
            loss_m = -float(cast(float, diffs[diffs < 0].sum()))
            elevation_loss_ft = round(loss_m * FT_PER_M, 1)

    return ActivityDetail(
        id=cast(int, row["id"]),
        name=row["name"],
        description=description or None,
        start_date=row["start_date"],
        distance_mi=row["distance_mi"],
        moving_time_s=row["moving_time_s"],
        elapsed_time_s=row["elapsed_time_s"],
        pace_min_per_mile=row["pace_min_per_mile"],
        avg_hr=int(row["avg_hr"]) if row["avg_hr"] is not None else None,
        min_hr=min_hr,
        median_hr=median_hr,
        max_hr=int(row["max_hr"]) if row["max_hr"] is not None else None,
        elevation_gain_ft=row["elevation_gain_ft"],
        elevation_loss_ft=elevation_loss_ft,
        temp_f_avg=row["temp_f_avg"],
        humidity_avg=row["humidity_avg"],
        run_type=row["run_type"],
        race_effort=row["race_effort"],
        workout_label=row["workout_label"],
        hr_excluded=row["hr_excluded_at"] is not None,
        wind_mph_avg=row["wind_mph_avg"],
        weather_attribution=WEATHER_ATTRIBUTION if row["temp_f_avg"] is not None else None,
        strava_url=row["strava_url"],
        polyline=polyline,
    )


# "easy" pinned to 100, not Strava's 0, so inference.py's workout_type = 0 pass skips it.
_RUN_TYPE_TO_WORKOUT_TYPE: dict[str, int] = {v: k for k, v in db.WORKOUT_TYPE_MAP.items()}
_RUN_TYPE_TO_WORKOUT_TYPE["easy"] = 100
_KNOWN_WORKOUT_LABELS: set[str] = {label for _, label in WORKOUT_LABEL_PATTERNS}


class ReclassifyRequest(TypedDict):
    run_type: NotRequired[str | None]
    race_effort: NotRequired[str | None]
    workout_label: NotRequired[str | None]
    hr_excluded: NotRequired[bool]


class ReclassifyResponse(TypedDict):
    run_type: str
    race_effort: str | None
    workout_label: str | None
    hr_excluded: bool


@app.post("/api/activity-reclassify")
def reclassify_activity(id: int, body: ReclassifyRequest) -> ReclassifyResponse:
    """
    Manual correction from activity.html; replays sync's downstream steps
    (lap fetch, label backfill, derive_all, plan auto-complete) immediately.
    """
    conn = _conn()
    row = conn.execute(
        "SELECT activity_id, name, DATE(start_date) AS date FROM activities WHERE activity_id = ?", [id]
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No activity {id}.")
    if row["date"] is not None:
        db.mark_derive_dirty(conn, row["date"])

    if "run_type" in body:
        run_type = body["run_type"]
        if run_type not in _RUN_TYPE_TO_WORKOUT_TYPE:
            raise HTTPException(status_code=400, detail=f"Invalid run_type: {run_type!r}")
        conn.execute(
            "UPDATE activities SET run_type = ?, workout_type = ?, run_type_inferred = NULL WHERE activity_id = ?",
            [run_type, _RUN_TYPE_TO_WORKOUT_TYPE[run_type], id],
        )

    if "race_effort" in body:
        race_effort = body["race_effort"]
        if race_effort is not None and race_effort not in ("raced", "hard", "casual"):
            raise HTTPException(status_code=400, detail=f"Invalid race_effort: {race_effort!r}")
        conn.execute(
            "UPDATE activities SET race_effort = ?, effort_ratio = NULL WHERE activity_id = ?",
            [race_effort, id],
        )

    if "workout_label" in body:
        workout_label = body["workout_label"]
        if workout_label is not None and workout_label not in _KNOWN_WORKOUT_LABELS:
            raise HTTPException(status_code=400, detail=f"Invalid workout_label: {workout_label!r}")
        conn.execute("UPDATE activities SET workout_label = ? WHERE activity_id = ?", [workout_label, id])

    if "hr_excluded" in body:
        db.set_hr_excluded(conn, id, body["hr_excluded"])

    conn.commit()

    effective_run_type = conn.execute(
        f"SELECT {db.EFFECTIVE_RUN_TYPE_SQL} AS t FROM activities WHERE activity_id = ?", [id]
    ).fetchone()["t"]

    if effective_run_type in ("workout", "race"):
        needs_laps = conn.execute(
            "SELECT laps_synced_at FROM activities WHERE activity_id = ?", [id]
        ).fetchone()["laps_synced_at"] is None
        if needs_laps:
            _fetch_interactive_laps(conn, id)

    if effective_run_type == "workout":
        current = conn.execute(
            "SELECT name, workout_label FROM activities WHERE activity_id = ?", [id]
        ).fetchone()
        if current["workout_label"] is None and current["name"]:
            label = classify_workout(current["name"])
            if label:
                conn.execute("UPDATE activities SET workout_label = ? WHERE activity_id = ?", [label, id])
                conn.commit()

    derive_all(conn)
    plan.auto_complete_plan(conn)
    conn.commit()

    final = conn.execute(
        "SELECT run_type, race_effort, workout_label, hr_excluded_at FROM activities WHERE activity_id = ?", [id]
    ).fetchone()
    return ReclassifyResponse(
        run_type=final["run_type"],
        race_effort=final["race_effort"],
        workout_label=final["workout_label"],
        hr_excluded=final["hr_excluded_at"] is not None,
    )


class LapMarker(TypedDict):
    lap_index: int
    time_s: float
    distance_mi: float | None
    median_pace_min_per_mile: float | None
    median_gap_min_per_mile: float | None
    median_hr: float | None
    own_distance_mi: float | None
    duration_s: int | None
    min_pace_min_per_mile: float | None
    max_pace_min_per_mile: float | None
    min_hr: float | None
    max_hr: float | None


class ActivityStreams(TypedDict):
    time_s: list[float]
    distance_mi: list[float | None]
    pace_min_per_mile: list[float | None]
    gap_min_per_mile: list[float | None]
    heartrate: list[float | None]
    elevation_ft: list[float | None]
    lap_markers: list[LapMarker]


def _speed_to_pace(mps: float | None) -> float | None:
    if mps is None or mps <= 0:
        return None
    return 26.8224 / mps


@app.get("/api/activity-streams")
def get_activity_streams(id: int) -> ActivityStreams:
    """
    Per-second time series for activity.html's dual-axis chart: pace, GAP,
    heartrate, elevation, all keyed by elapsed time_s, plus lap markers with
    per-window median pace/HR. Fetches and parquet-caches raw Strava streams
    on first call for a given activity (see streams.py); subsequent calls
    read the cache. 404s only if the activity itself doesn't exist.
    """
    conn = _conn()
    exists = conn.execute("SELECT laps_synced_at FROM activities WHERE activity_id = ?", [id]).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail=f"No activity {id}.")

    stale_laps = conn.execute(
        "SELECT 1 FROM laps WHERE activity_id = ? AND elapsed_time_s IS NULL LIMIT 1", [id]
    ).fetchone()
    if exists["laps_synced_at"] is None or stale_laps is not None:
        _fetch_interactive_laps(conn, id)

    df = streams.fetch_and_cache_streams(id)

    time_s = [float(v) for v in df["time_s"].tolist()]
    distance_mi = [float(v) / 1609.34 if v is not None and not pd.isna(v) else None for v in df["distance_m"].tolist()]
    pace_mpm = [_speed_to_pace(float(v)) if v is not None and not pd.isna(v) else None for v in df["velocity_smooth_mps"].tolist()]
    heartrate = [float(v) if v is not None and not pd.isna(v) else None for v in df["heartrate"].tolist()]
    elevation_ft = [float(v) * FT_PER_M if v is not None and not pd.isna(v) else None for v in df["altitude_m"].tolist()]

    has_velocity = bool(cast(pd.Series, df["velocity_smooth_mps"]).notna().any())
    has_grade = bool(cast(pd.Series, df["grade_smooth"]).notna().any())
    gap_mpm: list[float | None]
    if has_velocity and has_grade:
        gap_s_per_m = streams.compute_gap_pace_s_per_m(df)
        df = df.assign(gap_s_per_m=gap_s_per_m)
        gap_mpm = [
            float(v) * 1609.34 / 60.0 if v is not None and not pd.isna(v) else None
            for v in gap_s_per_m.tolist()
        ]
    else:
        gap_mpm = [None] * len(time_s)

    lap_rows = conn.execute("""
        SELECT lap_index, elapsed_time_s, distance_m
        FROM laps WHERE activity_id = ? ORDER BY lap_index
    """, [id]).fetchall()

    lap_markers: list[LapMarker] = []
    if lap_rows and all(r["elapsed_time_s"] is not None for r in lap_rows):
        cumulative = 0.0
        boundaries = [0.0]
        cum_distance_m = 0.0
        marker_meta: list[tuple[int, float, float, int]] = []
        for r in lap_rows:
            own_distance_m = float(r["distance_m"] or 0.0)
            duration_s = int(r["elapsed_time_s"])
            cumulative += float(duration_s)
            cum_distance_m += own_distance_m
            boundaries.append(cumulative)
            marker_meta.append((int(r["lap_index"]), cum_distance_m, own_distance_m, duration_s))

        pace_medians = streams.median_per_window(df, boundaries, "velocity_smooth_mps")
        hr_medians = streams.median_per_window(df, boundaries, "heartrate")
        gap_medians = (
            streams.median_per_window(df, boundaries, "gap_s_per_m")
            if "gap_s_per_m" in df.columns else [None] * (len(boundaries) - 1)
        )
        # Floor matches the chart's 25 min/mi axis clamp — excludes paused/GPS-glitch
        # near-zero-speed samples, which would otherwise blow out max pace (min speed).
        min_speed_mps = 26.8224 / 25.0
        velocity_stats = streams.min_median_max_per_window(df, boundaries, "velocity_smooth_mps", min_value=min_speed_mps)
        hr_stats = streams.min_median_max_per_window(df, boundaries, "heartrate")

        for (lap_index, cum_distance_m, own_distance_m, duration_s), pace_v, gap_v, hr_v, vstat, hstat, boundary_t in zip(
            marker_meta, pace_medians, gap_medians, hr_medians, velocity_stats, hr_stats, boundaries[1:]
        ):
            # velocity is inverted to pace, so its max speed -> min pace and vice versa.
            lap_markers.append(LapMarker(
                lap_index=lap_index,
                time_s=boundary_t,
                distance_mi=round(cum_distance_m / 1609.34, 2),
                median_pace_min_per_mile=_speed_to_pace(pace_v) if pace_v is not None else None,
                median_gap_min_per_mile=gap_v * 1609.34 / 60.0 if gap_v is not None else None,
                median_hr=hr_v,
                own_distance_mi=round(own_distance_m / 1609.34, 2),
                duration_s=duration_s,
                min_pace_min_per_mile=_speed_to_pace(vstat["max"]) if vstat["max"] is not None else None,
                max_pace_min_per_mile=_speed_to_pace(vstat["min"]) if vstat["min"] is not None else None,
                min_hr=hstat["min"],
                max_hr=hstat["max"],
            ))

    return ActivityStreams(
        time_s=time_s,
        distance_mi=distance_mi,
        pace_min_per_mile=pace_mpm,
        gap_min_per_mile=gap_mpm,
        heartrate=heartrate,
        elevation_ft=elevation_ft,
        lap_markers=lap_markers,
    )


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
    weather_attribution: str | None
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
    Repeated-session comparison for one race's build window (resolved as in
    /api/build-detail: detected build, else fixed distance-bucket window).
    Groups workout-type activities by workout_label (only
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
    race_row, detected, _weeks = _race_row_and_build(conn, race_date)
    build_start = detected["start"] if detected is not None else _window_fallback(race_date, race_row)[0]

    tc, tp = run_sport_filter()
    effective_run_type = db.effective_run_type_sql()
    day_rows = conn.execute(f"""
        SELECT
            activities.activity_id AS id,
            DATE(start_date) AS date,
            name,
            workout_label,
            {effective_run_type} AS run_type,
            ROUND(distance_m / 1609.34, 2) AS distance_mi,
            {db.pace_sql("average_speed_mps")} AS pace_min_per_mile,
            ROUND({db.hr_sql("average_heartrate")}) AS avg_hr,
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
                l.activity_id,
                COUNT(*) AS n_laps,
                SUM(l.distance_m) AS dist_m,
                SUM(l.moving_time_s) AS time_s,
                AVG({db.hr_sql("l.average_heartrate", "a")}) AS avg_hr
            FROM laps l JOIN activities a ON a.activity_id = l.activity_id
            WHERE l.activity_id IN ({placeholders})
              AND l.lap_type = 'work'
              AND l.moving_time_s >= {_WORK_LAP_MIN_TIME_S} AND l.distance_m >= {_WORK_LAP_MIN_DIST_M}
            GROUP BY l.activity_id
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
            weather_attribution=WEATHER_ATTRIBUTION if r["temp_f"] is not None else None,
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
    plan_id: int | None  # the completed plan this race closed out, if any


@app.get("/api/races")
def get_races() -> list[RaceRow]:
    """
    Every effective race at every distance, ascending by date, with
    per-category PR flags computed over the full history. plan_id is set
    when a completed plan's race_date/distance_bucket matched this race (see
    plan.completed_plans_by_race_date).
    """
    conn = _conn()
    plans_by_date = plan.completed_plans_by_race_date(conn)
    out: list[RaceRow] = []
    for r in race_rows(conn):
        pace = r["pace_min_per_mile"]
        race_date = cast(str, r["date"])
        matched_plan = plans_by_date.get(race_date)
        out.append(RaceRow(
            date=race_date,
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
            plan_id=matched_plan["plan_id"] if matched_plan is not None else None,
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
    tc, tp = run_sport_filter()
    today = date.today()
    current_year = today.year
    current_monday = monday_of(today)

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
            {db.monday_sql("start_date")} AS monday,
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
            last_monday = monday_of(date(year, 12, 31))
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
    quarter: str  # "2025-Q4", from the local start date
    hr_bucket: int  # bpm, bucket lower bound
    pace_bucket: int  # seconds/mile, bucket lower bound
    pace_bucket_heat_adj: int  # same, from the heat-adjusted pace (heat.py); equals pace_bucket without weather
    steady: bool  # long enough for HR to settle and not a warmup/cooldown/recovery lap (see _is_steady_lap)
    activity_id: int
    lap_index: int
    hr: float  # the lap's own average, before bucketing
    pace_s: float  # seconds/mile, the lap's own
    pace_s_heat_adj: float


class HrPaceActivity(TypedDict):
    activity_id: int
    name: str | None
    date: str  # local start date


class HrPaceHeatmap(TypedDict):
    points: list[HrPacePoint]
    activities: list[HrPaceActivity]  # each run with a point, once, so a clicked dot can list its runs


# A lap's average HR only describes its pace once HR has caught up with it:
# short reps average in the 2-3 min climb, and warmup/cooldown/recovery laps
# carry HR left over from the effort around them. Mixing those with steady
# miles bends the median line wherever workouts start, so the client can
# restrict the chart to steady laps.
_STEADY_LAP_MIN_TIME_S = 360
_STEADY_LAP_MIN_DIST_M = 1000
_UNSTEADY_LAP_TYPES = frozenset({"warmup", "cooldown", "recovery", "float"})


def _is_steady_lap(moving_time_s: float, distance_m: float, lap_type: str | None) -> bool:
    return (
        moving_time_s >= _STEADY_LAP_MIN_TIME_S
        and distance_m >= _STEADY_LAP_MIN_DIST_M
        and lap_type not in _UNSTEADY_LAP_TYPES
    )


@app.get("/api/hr-pace-heatmap")
def get_hr_pace_heatmap() -> HrPaceHeatmap:
    """
    One point per lap (avg HR, avg pace), bucketed into a 5bpm x 20s/mi grid.
    Excludes trivial laps (< 200m or < 45s) and laps missing HR. Returns raw
    points, not pre-aggregated cells, so the client can combine any set of
    selected years; each point names its run and lap, with the runs' names
    and dates listed once beside them.
    """
    conn = _conn()
    tc, tp = run_sport_filter()
    rows = conn.execute(f"""
        SELECT
            CAST(strftime('%Y', a.start_date) AS INTEGER) AS year,
            strftime('%Y', a.start_date) || '-Q' || ((CAST(strftime('%m', a.start_date) AS INTEGER) + 2) / 3) AS quarter,
            a.activity_id AS activity_id,
            a.name AS name,
            DATE(a.start_date) AS date,
            l.lap_index AS lap_index,
            l.average_heartrate AS hr,
            l.distance_m AS distance_m,
            l.moving_time_s AS moving_time_s,
            l.lap_type AS lap_type
        FROM laps l
        JOIN activities a ON a.activity_id = l.activity_id
        WHERE {tc} AND {db.hr_trusted_sql("a")}
          AND l.average_heartrate IS NOT NULL
          AND l.distance_m >= {LAP_MIN_DISTANCE_M}
          AND l.moving_time_s >= {LAP_MIN_MOVING_TIME_S}
    """, tp).fetchall()

    factors = heat.activity_heat_factors(conn, list({int(r["activity_id"]) for r in rows}))
    out: list[HrPacePoint] = []
    activities: dict[int, HrPaceActivity] = {}
    for r in rows:
        miles = r["distance_m"] / 1609.34
        pace_s_per_mile = r["moving_time_s"] / miles
        adj_pace_s_per_mile = pace_s_per_mile / factors.get(int(r["activity_id"]), 1.0)
        hr_bucket = int(r["hr"] // _HR_BUCKET_WIDTH) * _HR_BUCKET_WIDTH
        pace_bucket = int(pace_s_per_mile // _PACE_BUCKET_WIDTH_S) * _PACE_BUCKET_WIDTH_S
        adj_bucket = int(adj_pace_s_per_mile // _PACE_BUCKET_WIDTH_S) * _PACE_BUCKET_WIDTH_S
        out.append(HrPacePoint(
            year=r["year"], quarter=r["quarter"], hr_bucket=hr_bucket, pace_bucket=pace_bucket, pace_bucket_heat_adj=adj_bucket,
            steady=_is_steady_lap(r["moving_time_s"], r["distance_m"], r["lap_type"]),
            activity_id=int(r["activity_id"]), lap_index=int(r["lap_index"]), hr=round(float(r["hr"]), 1),
            pace_s=round(pace_s_per_mile, 1), pace_s_heat_adj=round(adj_pace_s_per_mile, 1),
        ))
        aid = int(r["activity_id"])
        if aid not in activities:
            activities[aid] = HrPaceActivity(activity_id=aid, name=r["name"], date=r["date"])
    return HrPaceHeatmap(points=out, activities=list(activities.values()))


class WorkbookEntry(TypedDict):
    name: str
    title: str
    uploaded: str  # ISO 8601 UTC


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _workbook_title(path: Path) -> str | None:
    # Workbooks are self-contained pages that can embed large inline datasets;
    # a bounded read keeps listing cheap and still covers any sane <head>.
    head = path.read_bytes()[:65536]
    m = _TITLE_RE.search(head)
    if m is None:
        return None
    title = html.unescape(m.group(1).decode("utf-8", errors="replace")).strip()
    return title or None


@app.get("/api/workbooks")
def list_workbooks() -> list[WorkbookEntry]:
    entries: list[WorkbookEntry] = []
    for p in _WORKBOOKS.glob("*.html"):
        # mtime is the upload time: the upload endpoint below is the only writer.
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        entries.append(
            WorkbookEntry(
                name=p.name,
                title=_workbook_title(p) or p.name,
                uploaded=mtime.isoformat(),
            )
        )
    entries.sort(key=lambda e: e["uploaded"], reverse=True)
    return entries


@app.post("/api/workbooks/upload")
async def upload_workbook(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename or not file.filename.endswith(".html"):
        raise HTTPException(status_code=400, detail="Only .html files are accepted")
    dest = _WORKBOOKS / Path(file.filename).name
    content = await file.read()
    await anyio.to_thread.run_sync(dest.write_bytes, content)
    return JSONResponse({"name": dest.name})


class SyncTriggerResponse(TypedDict):
    status: str


def _run_sync_in_background(history: bool) -> None:
    """Thread target for /api/sync: opens its own connection (sqlite3.Connection
    isn't safe to share across threads) and runs the same retry loop miles-sync
    uses; progress is exposed via _sync_progress, polled by
    GET /api/sync/status. The history pass can wait out many 15-minute Strava
    windows, so only an explicit request runs it; the scheduled miles-sync owns
    it otherwise. Any exception (network down, bad credentials, a real bug)
    is caught here so it can't take the server down or leave the progress state
    stuck at "running" forever."""
    try:
        with db.connection() as conn:
            new_count = sync.run_sync(conn, full=False, history=history, progress=_sync_progress)
        _sync_progress.finish(new_count)
    except Exception as e:
        _logger.exception("Background sync failed")
        _sync_progress.fail(describe_error(e))


@app.post("/api/sync")
def trigger_sync(history: bool = False) -> SyncTriggerResponse:
    if not _sync_progress.try_start():
        return SyncTriggerResponse(status="running")
    threading.Thread(target=_run_sync_in_background, args=(history,), daemon=True).start()
    return SyncTriggerResponse(status="started")


@app.get("/api/sync/status")
def sync_status() -> sync.SyncStatus:
    return _sync_progress.snapshot()


# Must precede the catch-all static mount below.
app.include_router(areas_api_router)
app.include_router(candidates_api_router)
app.include_router(distance_builds_router)
app.include_router(fitness_api_router)
app.include_router(health_api_router)
app.include_router(easy_efficiency_router)
app.include_router(load_api_router)
app.include_router(plan_api_router)
app.include_router(routes_api_router)
app.include_router(searches_api_router)
app.include_router(terrain_api_router)

# Lift the MCP transport's exact-path /mcp route onto this app directly.
# A nested app.mount() would either double the path (/mcp/mcp) or leave the
# exact path reachable only via a trailing-slash redirect that not every
# MCP client follows.
app.router.routes.extend(_mcp_app.routes)

class _NoCacheStaticFiles(StaticFiles):
    """Static pages are edited live (uvicorn reloads, browser refreshes) —
    no-cache forces the browser to revalidate every file on every load
    (cheap 304s locally) instead of heuristically serving stale copies,
    which otherwise requires a hard reload to pick up UI changes."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/workbooks", _NoCacheStaticFiles(directory=str(_WORKBOOKS)), name="workbooks")
app.mount("/", _NoCacheStaticFiles(directory=str(_STATIC), html=True), name="static")


@click.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=8000, type=int, help="Port to bind to.")
@click.option("--reload/--no-reload", default=True, help="Enable auto-reload on code changes.")
def main(host: str, port: int, reload: bool) -> None:
    # Watch only the package (code + static) — the default watches the whole
    # project dir, and the reloader dies racing transient files that uv/pyright
    # create under .venv.
    reload_dirs = [str(Path(__file__).parent)] if reload else None
    uvicorn.run("miles.api:app", host=host, port=port, reload=reload, reload_dirs=reload_dirs)
