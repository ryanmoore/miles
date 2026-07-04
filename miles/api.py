import sqlite3
import uvicorn
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict, cast

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .builds import Build, RaceRef, detect_builds
from .derive import ensure_derived
from .format import fmt_pace as _fmt_pace
from .format import fmt_time as _fmt_time
from .periods import Gap, Period, WeekAgg, _is_active, _zero_fill, detect_periods
from .races import MARATHON_MAX_M, MARATHON_MIN_M, classify_race_distance, race_rows

app = FastAPI(title="miles")

_BUILD_WEEKS = 12
_RUN_TYPES = ("Run", "TrailRun", "VirtualRun")
_STATIC = Path(__file__).parent / "static"


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


@app.get("/api/marathons")
def get_marathons(build_weeks: int = _BUILD_WEEKS) -> list[MarathonRow]:
    """
    All marathon race results with stats for the build_weeks-week training
    block that preceded each one. Sorted by date ascending.
    """
    conn = _conn()
    tc, tp = _type_clause()

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
        build_start: str = conn.execute(
            "SELECT DATE(?, ?)", (race_date, f"-{build_weeks * 7} days")
        ).fetchone()[0]

        by_type_rows = conn.execute(f"""
            SELECT
                run_type,
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
            GROUP BY run_type
            ORDER BY run_type
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
            finish_time=_fmt_time(race["moving_time_s"]),
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
            finish_time=_fmt_time(race["moving_time_s"]),
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

    weeks: list[WeekAgg] = [
        {"monday": r["monday"], "miles": r["miles"] or 0.0, "runs": r["runs"], "workouts": r["workouts"] or 0}
        for r in week_rows
    ]
    filled_weeks = _zero_fill(weeks)
    periods, gaps = detect_periods(weeks)

    race_rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, name, distance_m, race_effort
        FROM activities
        WHERE {tc} AND {effective_run_type} = 'race'
        ORDER BY date
    """, tp).fetchall()

    races = [
        RaceMarker(
            date=r["date"],
            name=r["name"],
            distance_category=classify_race_distance(r["distance_m"]) or "other",
            effort=r["race_effort"],
        )
        for r in race_rows
    ]
    race_refs: list[RaceRef] = [
        {
            "date": r["date"],
            "name": r["name"],
            "distance_category": classify_race_distance(r["distance_m"]) or "other",
            "distance_m": r["distance_m"],
        }
        for r in race_rows
        if r["distance_m"] is not None
    ]
    builds = detect_builds(weeks, race_refs, periods) if periods else []

    return WeeklyHistory(
        weeks=[HistoryWeek(monday=w["monday"], miles=w["miles"], runs=w["runs"]) for w in filled_weeks],
        periods=periods,
        gaps=gaps,
        builds=builds,
        races=races,
    )


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
            pace_min_per_mile=_fmt_pace(pace) if isinstance(pace, (int, float)) else None,
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
        if _is_active(wk):
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


app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")


def main() -> None:
    uvicorn.run("miles.api:app", host="127.0.0.1", port=8000, reload=True)
