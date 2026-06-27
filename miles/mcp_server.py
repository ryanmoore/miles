import json
import sqlite3

from mcp.server.fastmcp import FastMCP

from . import db

mcp = FastMCP("miles")

RUN_TYPES = ("Run", "TrailRun", "VirtualRun")


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _run_type_filter(sport_types: tuple[str, ...] = RUN_TYPES) -> tuple[str, list[str]]:
    placeholders = ",".join("?" * len(sport_types))
    return f"sport_type IN ({placeholders})", list(sport_types)


@mcp.tool()
def get_weekly_mileage(start_date: str | None = None, end_date: str | None = None) -> str:
    """
    Weekly running mileage grouped by ISO week (YYYY-Www).
    Optionally filter by start_date / end_date (YYYY-MM-DD).
    Returns list of {week, miles, runs}.
    """
    conn = _conn()
    type_clause, params = _run_type_filter()
    where = f"WHERE {type_clause}"
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND start_date <= ?"
        params.append(end_date)

    rows = conn.execute(f"""
        SELECT
            strftime('%Y-W%W', start_date) AS week,
            ROUND(SUM(distance_m) / 1609.34, 2)  AS miles,
            COUNT(*) AS runs
        FROM activities
        {where}
        GROUP BY week
        ORDER BY week
    """, params).fetchall()
    return json.dumps([dict(r) for r in rows])


@mcp.tool()
def get_activities(
    run_type: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> str:
    """
    List individual runs with key stats.
    run_type: 'easy' | 'workout' | 'long_run' | 'race' | None for all.
    Dates are YYYY-MM-DD. Returns up to `limit` rows, newest first.
    """
    conn = _conn()
    type_clause, params = _run_type_filter()
    where = f"WHERE {type_clause}"
    if run_type:
        where += " AND run_type = ?"
        params.append(run_type)
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND start_date <= ?"
        params.append(end_date)

    rows = conn.execute(f"""
        SELECT
            activity_id,
            name,
            start_date,
            run_type,
            ROUND(distance_m / 1609.34, 2) AS miles,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            average_heartrate,
            total_elevation_gain_m,
            strava_url
        FROM activities
        {where}
        ORDER BY start_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    return json.dumps([dict(r) for r in rows])


@mcp.tool()
def get_training_block(start_date: str, end_date: str) -> str:
    """
    Aggregate stats for a training block (date range), broken down by run_type.
    Dates are YYYY-MM-DD. Good for comparing base vs. build phases.
    """
    conn = _conn()
    type_clause, base_params = _run_type_filter()
    date_clause = "AND start_date >= ? AND start_date <= ?"
    date_params = [start_date, end_date]

    by_type = conn.execute(f"""
        SELECT
            run_type,
            COUNT(*) AS runs,
            ROUND(SUM(distance_m) / 1609.34, 2)  AS total_miles,
            ROUND(AVG(distance_m) / 1609.34, 2)  AS avg_miles,
            ROUND(AVG(average_heartrate), 1)       AS avg_hr,
            CASE WHEN AVG(average_speed_mps) > 0
                 THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                 ELSE NULL END                     AS avg_pace_min_per_mile,
            ROUND(SUM(total_elevation_gain_m) * 3.28084, 0) AS total_elevation_ft
        FROM activities
        WHERE {type_clause} {date_clause}
        GROUP BY run_type
        ORDER BY run_type
    """, base_params + date_params).fetchall()

    totals = conn.execute(f"""
        SELECT
            COUNT(*) AS runs,
            ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles,
            ROUND(SUM(total_elevation_gain_m) * 3.28084, 0) AS total_elevation_ft
        FROM activities
        WHERE {type_clause} {date_clause}
    """, base_params + date_params).fetchone()

    return json.dumps({
        "period": {"start": start_date, "end": end_date},
        "total": dict(totals),
        "by_type": [dict(r) for r in by_type],
    })


MARATHON_MIN_M = 42000.0
MARATHON_MAX_M = 43500.0


@mcp.tool()
def get_marathon_comparison(build_weeks: int = 12) -> str:
    """
    For every tagged marathon race, returns the result alongside stats for
    the build_weeks-week training block that preceded it.
    Sorted by date ascending. build_weeks defaults to 12.

    Each entry has:
      name, date, finish_time_s, distance_miles, pace_min_per_mile,
      build: { start, end, weeks, total_miles, avg_mpw,
               by_type: { easy, workout, long_run, race? } }
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()

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

    out = []
    for race in races:
        race_date: str = race["race_date"]
        build_start: str = conn.execute(
            "SELECT DATE(?, ?)", (race_date, f"-{build_weeks * 7} days")
        ).fetchone()[0]

        by_type = conn.execute(f"""
            SELECT
                run_type,
                COUNT(*) AS runs,
                ROUND(SUM(distance_m) / 1609.34, 2)         AS total_miles,
                ROUND(AVG(distance_m) / 1609.34, 2)         AS avg_miles,
                ROUND(AVG(average_heartrate), 1)             AS avg_hr,
                CASE WHEN AVG(average_speed_mps) > 0
                     THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                     ELSE NULL END                           AS avg_pace_min_per_mile
            FROM activities
            WHERE {type_clause}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
            GROUP BY run_type
            ORDER BY run_type
        """, type_params + [build_start, race_date]).fetchall()

        totals = conn.execute(f"""
            SELECT
                COUNT(*) AS runs,
                ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles
            FROM activities
            WHERE {type_clause}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
        """, type_params + [build_start, race_date]).fetchone()

        total_miles: float = totals["total_miles"] or 0.0

        out.append({
            "name": race["name"],
            "date": race_date,
            "finish_time_s": race["moving_time_s"],
            "distance_miles": race["distance_miles"],
            "pace_min_per_mile": race["pace_min_per_mile"],
            "build": {
                "start": build_start,
                "end": race_date,
                "weeks": build_weeks,
                "total_miles": total_miles,
                "avg_mpw": round(total_miles / build_weeks, 1),
                "by_type": {
                    row["run_type"]: {
                        "runs": row["runs"],
                        "total_miles": row["total_miles"],
                        "avg_miles": row["avg_miles"],
                        "avg_hr": row["avg_hr"],
                        "avg_pace_min_per_mile": row["avg_pace_min_per_mile"],
                    }
                    for row in by_type
                    if row["run_type"] is not None
                },
            },
        })

    return json.dumps(out)


@mcp.tool()
def get_workout_laps(
    workout_label: str | None = None,
    name_contains: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> str:
    """
    Workout sessions with per-lap breakdown. Useful for cross-build quality comparisons.
    workout_label: classifier label e.g. 'LT', 'MP Flux', 'Tempo', 'Strides'.
    name_contains: substring match on activity name (fallback if no label set).
    Returns newest-first up to `limit` sessions. Each session includes:
      activity_id, name, date, workout_label,
      laps: [{lap_index, distance_miles, pace_min_per_mile, avg_hr, max_hr}]
    """
    conn = _conn()
    type_clause, params = _run_type_filter()
    where = f"WHERE {type_clause} AND run_type = 'workout'"
    if workout_label:
        where += " AND workout_label = ?"
        params.append(workout_label)
    if name_contains:
        where += " AND name LIKE ?"
        params.append(f"%{name_contains}%")
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND start_date <= ?"
        params.append(end_date)

    activities = conn.execute(f"""
        SELECT activity_id, name, DATE(start_date) AS date, workout_label
        FROM activities
        {where}
        ORDER BY start_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    out = []
    for act in activities:
        laps = conn.execute("""
            SELECT
                lap_index,
                ROUND(distance_m / 1609.34, 3) AS distance_miles,
                CASE WHEN average_speed_mps > 0
                     THEN ROUND(26.8224 / average_speed_mps, 2)
                     ELSE NULL END AS pace_min_per_mile,
                average_heartrate AS avg_hr,
                max_heartrate AS max_hr
            FROM laps
            WHERE activity_id = ?
            ORDER BY lap_index
        """, [act["activity_id"]]).fetchall()
        out.append({
            "activity_id": act["activity_id"],
            "name": act["name"],
            "date": act["date"],
            "workout_label": act["workout_label"],
            "laps": [dict(lap) for lap in laps],
        })

    return json.dumps(out)


@mcp.tool()
def run_sql(query: str) -> str:
    """
    Run a read-only SQL SELECT against the database.
    Use this for ad-hoc questions the other tools don't cover.

    Table: activities
      activity_id, name, sport_type, start_date, workout_type, run_type, workout_label,
      distance_m, moving_time_s, elapsed_time_s, total_elevation_gain_m,
      average_speed_mps, max_speed_mps, average_heartrate, max_heartrate,
      average_cadence, gear_id, strava_url, synced_at

    Table: laps  (one row per lap; only workout activities are synced)
      lap_id, activity_id, lap_index, distance_m, moving_time_s, average_speed_mps,
      average_heartrate, max_heartrate, average_cadence, total_elevation_gain_m, pace_zone
    """
    stripped = query.strip().upper().lstrip("(")
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        return json.dumps({"error": "Only SELECT / WITH queries are permitted."})
    conn = _conn()
    try:
        rows = conn.execute(query).fetchall()
        return json.dumps([dict(r) for r in rows])
    except Exception as e:
        return json.dumps({"error": str(e)})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
