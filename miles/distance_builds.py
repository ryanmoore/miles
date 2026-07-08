"""Distance-specific build analysis: generalizes /api/marathons and
/api/marathon-weeks (see api.py) to other race distances — 5K, 10K, half,
marathon, 50K, and everything else — for a marathon-focused runner who also
wants to see those builds.
"""

import sqlite3
from datetime import date, timedelta
from typing import Literal, cast

from typing_extensions import TypedDict

from fastapi import APIRouter

from . import db
from .build_paces import PaceClaim, pace_claims
from .derive import ensure_derived
from .races import RUN_SPORT_TYPES, race_rows

router = APIRouter()

Bucket = Literal["5K", "10K", "Half", "Marathon", "50K", "Other"]

# Shorter races warrant shorter, distance-appropriate build windows than the
# marathon's 12 weeks; 50K gets the same 12-week window as the marathon.
_BUILD_WEEKS: dict[Bucket, int] = {
    "5K": 8, "10K": 8, "Half": 10, "Marathon": 12, "50K": 12, "Other": 8,
}
# races.py's classify_race_distance() categories for each bucket (lowercase
# "half"/"marathon"; "Other" covers 15K/10M/30K/unmatched distances, which
# race_rows() lumps together as "other").
_DISTANCE_CATEGORY: dict[Bucket, str] = {
    "5K": "5K", "10K": "10K", "Half": "half",
    "Marathon": "marathon", "50K": "50K", "Other": "other",
}


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    ensure_derived(conn)
    return conn


def _type_clause() -> tuple[str, list[str]]:
    ph = ",".join("?" * len(RUN_SPORT_TYPES))
    return f"sport_type IN ({ph})", list(RUN_SPORT_TYPES)


def _build_start(race_date: str, build_weeks: int) -> date:
    """Monday-aligned build_start, so every build spans whole weeks with no
    partially-cut-off week — the build-window convention used app-wide."""
    race_dt = date.fromisoformat(race_date)
    race_week_monday = race_dt - timedelta(days=race_dt.weekday())
    return race_week_monday - timedelta(weeks=build_weeks)


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


class DistanceBuildRow(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    distance_miles: float | None
    pace_min_per_mile: float | None
    build: BuildStat
    build_weeks: int


@router.get("/api/distance-builds")
def get_distance_builds(bucket: Bucket) -> list[DistanceBuildRow]:
    """
    All races at the given distance bucket (5K/10K/Half) with stats for the
    bucket's build window preceding each one. Sorted by date ascending,
    mirroring /api/marathons.
    """
    build_weeks = _BUILD_WEEKS[bucket]
    conn = _conn()
    tc, tp = _type_clause()

    effective_run_type = db.effective_run_type_sql()

    out: list[DistanceBuildRow] = []
    for race in race_rows(conn, distance_category=_DISTANCE_CATEGORY[bucket]):
        race_date = cast(str, race["date"])
        build_start_s = _build_start(race_date, build_weeks).isoformat()

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
        """, tp + [build_start_s, race_date]).fetchall()

        totals = conn.execute(f"""
            SELECT ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles
            FROM activities
            WHERE {tc}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
        """, tp + [build_start_s, race_date]).fetchone()

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
        """, tp + [build_start_s, race_date]).fetchone()

        total_miles: float = totals["total_miles"] or 0.0

        out.append(DistanceBuildRow(
            name=cast("str | None", race["name"]),
            date=race_date,
            finish_time_s=cast("int | None", race["finish_time_s"]),
            finish_time=cast(str, race["finish_time"]),
            distance_miles=cast("float | None", race["distance_miles"]),
            pace_min_per_mile=cast("float | None", race["pace_min_per_mile"]),
            build=BuildStat(
                start=build_start_s,
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
                pace_claims=pace_claims(conn, build_start_s, race_date),
            ),
            build_weeks=build_weeks,
        ))

    return out


class WeekPoint(TypedDict):
    offset: int
    miles: float


class DistanceBuildWeeks(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    weeks: list[WeekPoint]
    build_weeks: int


@router.get("/api/distance-build-weeks")
def get_distance_build_weeks(bucket: Bucket) -> list[DistanceBuildWeeks]:
    """
    Weekly mileage for each build at the given distance bucket, with each
    week expressed as an offset from race day (0 = race week). Mirrors
    /api/marathon-weeks.
    """
    build_weeks = _BUILD_WEEKS[bucket]
    conn = _conn()
    tc, tp = _type_clause()

    out: list[DistanceBuildWeeks] = []
    for race in race_rows(conn, distance_category=_DISTANCE_CATEGORY[bucket]):
        race_date = cast(str, race["date"])
        build_start_s = _build_start(race_date, build_weeks).isoformat()

        # Offset formula anchored to build_start (always a Monday):
        # 0 = race week, -1 = week before, -build_weeks = first build week.
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
        """, [build_start_s] + tp + [build_start_s, race_date]).fetchall()

        out.append(DistanceBuildWeeks(
            name=cast("str | None", race["name"]),
            date=race_date,
            finish_time_s=cast("int | None", race["finish_time_s"]),
            finish_time=cast(str, race["finish_time"]),
            weeks=[WeekPoint(offset=row["week_offset"], miles=row["miles"]) for row in week_rows],
            build_weeks=build_weeks,
        ))

    return out
