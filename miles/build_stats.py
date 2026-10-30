"""Window statistics for a training build: per-run-type breakdown, total and
peak mileage, and weekly mileage keyed by offset from race week."""

import sqlite3

from typing_extensions import TypedDict

from . import db
from .build_paces import PaceClaim, pace_claims
from .races import run_sport_filter


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


class WeekPoint(TypedDict):
    offset: int
    miles: float


def run_type_stats(conn: sqlite3.Connection, start: str, end: str, run_type_expr: str) -> dict[str, RunTypeStat]:
    """Per-run-type stats over run dates in [start, end), grouped by
    `run_type_expr` (a SQL expression: the effective type or the raw column)."""
    tc, tp = run_sport_filter()
    rows = conn.execute(f"""
        SELECT
            {run_type_expr} AS run_type,
            COUNT(*)                                     AS runs,
            ROUND(SUM(distance_m) / 1609.34, 2)         AS total_miles,
            ROUND(AVG(distance_m) / 1609.34, 2)         AS avg_miles,
            ROUND(AVG({db.hr_sql("average_heartrate")}), 1) AS avg_hr,
            {db.pace_sql("AVG(average_speed_mps)")}      AS avg_pace_min_per_mile
        FROM activities
        WHERE {tc}
          AND DATE(start_date) >= ?
          AND DATE(start_date) < ?
        GROUP BY {run_type_expr}
        ORDER BY 1
    """, tp + [start, end]).fetchall()
    return {
        row["run_type"]: RunTypeStat(
            runs=row["runs"],
            total_miles=row["total_miles"],
            avg_miles=row["avg_miles"],
            avg_hr=row["avg_hr"],
            avg_pace_min_per_mile=row["avg_pace_min_per_mile"],
        )
        for row in rows
        if row["run_type"] is not None
    }


def window_total_miles(conn: sqlite3.Connection, start: str, end: str) -> float:
    tc, tp = run_sport_filter()
    row = conn.execute(f"""
        SELECT ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles
        FROM activities
        WHERE {tc}
          AND DATE(start_date) >= ?
          AND DATE(start_date) < ?
    """, tp + [start, end]).fetchone()
    return row["total_miles"] or 0.0


def peak_weeks(conn: sqlite3.Connection, start: str, end: str) -> tuple[float | None, float | None]:
    """(peak calendar-week miles, peak rolling-3-week average) over run dates in [start, end)."""
    tc, tp = run_sport_filter()
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
    """, tp + [start, end]).fetchone()
    return peak["peak_week"], peak["peak_3wk_avg"]


def build_stat(conn: sqlite3.Connection, build_start: str, race_date: str, build_weeks: int) -> BuildStat:
    total_miles = window_total_miles(conn, build_start, race_date)
    peak_week, peak_3wk_avg = peak_weeks(conn, build_start, race_date)
    return BuildStat(
        start=build_start,
        weeks=build_weeks,
        total_miles=total_miles,
        avg_mpw=round(total_miles / build_weeks, 1),
        peak_week=peak_week,
        peak_3wk_avg=peak_3wk_avg,
        by_type=run_type_stats(conn, build_start, race_date, db.effective_run_type_sql()),
        pace_claims=pace_claims(conn, build_start, race_date),
    )


def week_offset_points(conn: sqlite3.Connection, build_start: str, race_date: str, build_weeks: int) -> list[WeekPoint]:
    """Weekly miles keyed by offset from race week (0 = race week, -build_weeks =
    first build week). build_start is a Monday and the anchor of the offset
    arithmetic, which keeps every julianday difference positive so CAST
    truncates correctly."""
    tc, tp = run_sport_filter()
    rows = conn.execute(f"""
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
    return [WeekPoint(offset=row["week_offset"], miles=row["miles"]) for row in rows]
