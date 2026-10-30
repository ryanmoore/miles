"""Shared race-distance taxonomy: one place that answers "what race distance is this?"

GPS reads long on certified courses, so bucket ranges skew above nominal distance.
"""

import sqlite3
from typing_extensions import TypedDict

from . import db
from .builds import Build, RaceRef, detect_builds
from .format import fmt_time
from .periods import Period, WeekAgg, detect_periods

RUN_SPORT_TYPES: tuple[str, ...] = ("Run", "TrailRun", "VirtualRun")


def run_sport_filter(alias: str = "", exclude: tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """A WHERE condition selecting run activities (minus any `exclude`d sport
    types), optionally qualified by a table alias, and its parameters."""
    prefix = f"{alias}." if alias else ""
    sports = [t for t in RUN_SPORT_TYPES if t not in exclude]
    return f"{prefix}sport_type IN ({','.join('?' * len(sports))})", sports

RACE_BUCKETS: list[tuple[str, float, float]] = [
    ("5K", 4800, 5600),
    ("10K", 9700, 11200),
    ("15K", 14700, 15800),
    ("10M", 15900, 17000),
    ("half", 20700, 22300),
    ("30K", 29300, 31500),
    ("marathon", 42000, 43500),
    ("50K", 45000, 52000),
]

NOMINAL_METERS: dict[str, float] = {
    "5K": 5000.0,
    "10K": 10000.0,
    "15K": 15000.0,
    "10M": 16093.4,
    "half": 21097.5,
    "30K": 30000.0,
    "marathon": 42195.0,
    "50K": 50000.0,
}

MARATHON_MIN_M: float = 42000.0
MARATHON_MAX_M: float = 43500.0


def classify_race_distance(distance_m: float | None) -> str | None:
    """Return the category of the first RACE_BUCKETS entry containing distance_m (inclusive), else None."""
    if distance_m is None:
        return None
    for category, min_m, max_m in RACE_BUCKETS:
        if min_m <= distance_m <= max_m:
            return category
    return None


def riegel_time(t_s: float, from_m: float, to_m: float, exponent: float = 1.06) -> float:
    """Riegel formula: predicted time at to_m given a time at from_m."""
    return t_s * (to_m / from_m) ** exponent


def race_rows(
    conn: sqlite3.Connection,
    distance_category: str | None = None,
    start_date: str | None = None,
    activity_id: int | None = None,
) -> list[dict[str, object]]:
    """
    All effective-race activities, ascending by date. PR flags are computed over
    the athlete's full race history first; distance_category/start_date/activity_id
    filter the returned rows afterward, so filtering never changes an is_pr verdict.
    """
    effective_run_type = db.effective_run_type_sql("a")
    sport_clause, sport_params = run_sport_filter("a")
    where = f"WHERE {sport_clause} AND {effective_run_type} = 'race'"

    rows = conn.execute(f"""
        SELECT
            a.activity_id,
            a.name,
            DATE(a.start_date) AS date,
            a.distance_m,
            ROUND(a.distance_m / 1609.34, 2) AS distance_miles,
            a.moving_time_s,
            {db.pace_sql("a.average_speed_mps")} AS pace_min_per_mile,
            ROUND({db.hr_sql("a.average_heartrate", "a")}, 1) AS avg_hr,
            CASE WHEN a.workout_type = 0 AND a.run_type_inferred IS NOT NULL
                 THEN 'inferred' ELSE 'strava' END AS run_type_source,
            a.race_effort,
            a.effort_ratio,
            a.strava_url
        FROM activities a
        {where}
        ORDER BY date ASC
    """, sport_params).fetchall()

    best_finish_s: dict[str, int] = {}
    out: list[dict[str, object]] = []
    for r in rows:
        category: str = classify_race_distance(r["distance_m"]) or "other"
        finish_time_s: int | None = r["moving_time_s"]
        is_pr = False
        if category != "other" and finish_time_s is not None:
            best = best_finish_s.get(category)
            if best is None or finish_time_s < best:
                is_pr = True
                best_finish_s[category] = finish_time_s

        if distance_category is not None and category != distance_category:
            continue
        race_date: str = r["date"]
        if start_date is not None and race_date < start_date:
            continue
        if activity_id is not None and r["activity_id"] != activity_id:
            continue

        out.append({
            "activity_id": r["activity_id"],
            "name": r["name"],
            "date": race_date,
            "distance_category": category,
            "distance_miles": r["distance_miles"],
            "finish_time_s": finish_time_s,
            "finish_time": fmt_time(finish_time_s),
            "pace_min_per_mile": r["pace_min_per_mile"],
            "avg_hr": r["avg_hr"],
            "run_type_source": r["run_type_source"],
            "effort": r["race_effort"],
            "effort_ratio": r["effort_ratio"],
            "is_pr": is_pr,
            "strava_url": r["strava_url"],
        })
    return out


def marathon_races(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Marathon-length activities tagged as races in Strava, ascending by date."""
    return conn.execute(f"""
        SELECT
            name,
            DATE(start_date) AS race_date,
            ROUND(distance_m / 1609.34, 2) AS distance_miles,
            moving_time_s,
            {db.pace_sql("average_speed_mps")} AS pace_min_per_mile
        FROM activities
        WHERE run_type = 'race'
          AND distance_m BETWEEN ? AND ?
        ORDER BY race_date
    """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchall()


def week_aggs(conn: sqlite3.Connection, start_date: str | None = None) -> list[WeekAgg]:
    """Every calendar week with at least one run (from start_date on, if given),
    Monday-aligned: the input detect_periods and detect_builds take."""
    sport_clause, params = run_sport_filter()
    where = f"WHERE {sport_clause}"
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN {db.effective_run_type_sql()} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        {where}
        GROUP BY monday
        ORDER BY monday
    """, params).fetchall()
    return [
        {"monday": r["monday"], "miles": r["miles"] or 0.0, "runs": r["runs"], "workouts": r["workouts"] or 0}
        for r in rows
    ]


class RaceActivityRow(TypedDict):
    date: str
    name: str | None
    distance_m: float | None
    distance_miles: float | None
    moving_time_s: int | None
    race_effort: str | None


def effective_race_activities(conn: sqlite3.Connection) -> list[RaceActivityRow]:
    """Every effective-race run, ascending by date."""
    sport_clause, params = run_sport_filter()
    rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, name, distance_m, ROUND(distance_m / 1609.34, 2) AS distance_miles,
               moving_time_s, race_effort
        FROM activities
        WHERE {sport_clause} AND {db.effective_run_type_sql()} = 'race'
        ORDER BY date
    """, params).fetchall()
    return [
        {
            "date": r["date"],
            "name": r["name"],
            "distance_m": r["distance_m"],
            "distance_miles": r["distance_miles"],
            "moving_time_s": r["moving_time_s"],
            "race_effort": r["race_effort"],
        }
        for r in rows
    ]


def race_refs(rows: list[RaceActivityRow]) -> list[RaceRef]:
    return [
        {
            "date": r["date"],
            "name": r["name"],
            "distance_category": classify_race_distance(r["distance_m"]) or "other",
            "distance_m": r["distance_m"],
        }
        for r in rows
        if r["distance_m"] is not None
    ]


def full_periods_and_builds(conn: sqlite3.Connection) -> tuple[list[Period], list[Build], list[WeekAgg]]:
    """Periods, race-anchored builds, and the weekly aggregates they came from,
    over the whole history, for locating the one enclosing an arbitrary race date."""
    weeks = week_aggs(conn)
    periods, _gaps = detect_periods(weeks)
    if not periods:
        return [], [], weeks
    return periods, detect_builds(weeks, race_refs(effective_race_activities(conn)), periods), weeks
