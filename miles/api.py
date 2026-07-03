import sqlite3
import uvicorn
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .races import MARATHON_MAX_M, MARATHON_MIN_M

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
    return conn


def _type_clause() -> tuple[str, list[str]]:
    ph = ",".join("?" * len(_RUN_TYPES))
    return f"sport_type IN ({ph})", list(_RUN_TYPES)


def _fmt_time(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


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


app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")


def main() -> None:
    uvicorn.run("miles.api:app", host="127.0.0.1", port=8000, reload=True)
