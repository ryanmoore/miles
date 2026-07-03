import json
import sqlite3
import statistics
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from . import db
from .classifier import LapType, classify_laps
from .races import MARATHON_MAX_M, MARATHON_MIN_M

mcp = FastMCP("miles")

RUN_TYPES = ("Run", "TrailRun", "VirtualRun")


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _classify_lap_rows(laps: list[sqlite3.Row]) -> list[LapType]:
    """Run the lap classifier over sqlite rows (need average_speed_mps, distance_m, average_heartrate)."""
    return classify_laps(
        speeds=[float(row["average_speed_mps"]) for row in laps],
        distances_m=[float(row["distance_m"]) for row in laps],
        heartrates=[float(row["average_heartrate"]) if row["average_heartrate"] is not None else None
                    for row in laps],
    )


_PACE_KEYS = frozenset({
    "pace_min_per_mile", "avg_pace_min_per_mile", "avg_pace",
    "avg_rep_pace", "best_rep_pace", "pace_min_mi", "avg_pace_min_mi",
})


def _pace_str(v: float) -> str:
    """Convert decimal minutes-per-mile (e.g. 6.56) to MM:SS string (e.g. '6:34')."""
    mins = int(v)
    secs = round((v - mins) * 60)
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}"


def _fmt_paces(obj: object) -> object:
    """Recursively convert known pace keys from decimal float to MM:SS string."""
    if isinstance(obj, dict):
        return {
            k: (_pace_str(float(v)) if k in _PACE_KEYS and isinstance(v, (int, float)) else _fmt_paces(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_fmt_paces(item) for item in obj]
    return obj


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
    List individual runs with key stats, including weather conditions when available.
    run_type: 'easy' | 'workout' | 'long_run' | 'race' | None for all.
    Dates are YYYY-MM-DD. Returns up to `limit` rows, newest first.
    Weather fields (temp_c_start, temp_c_max, apparent_temp_c_max, humidity_avg,
    precip_mm, wind_kph_avg) are null if not yet synced for that activity.
    """
    conn = _conn()
    _, sport_params = _run_type_filter()
    placeholders = ",".join("?" * len(sport_params))
    conditions = [f"a.sport_type IN ({placeholders})"]
    params: list[str | int] = list(sport_params)
    if run_type:
        conditions.append("a.run_type = ?")
        params.append(run_type)
    if start_date:
        conditions.append("a.start_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("a.start_date <= ?")
        params.append(end_date)
    where = "WHERE " + " AND ".join(conditions)

    rows = conn.execute(f"""
        SELECT
            a.activity_id,
            a.name,
            a.start_date,
            a.run_type,
            ROUND(a.distance_m / 1609.34, 2) AS miles,
            a.moving_time_s,
            CASE WHEN a.average_speed_mps > 0
                 THEN ROUND(26.8224 / a.average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            a.average_heartrate,
            a.total_elevation_gain_m,
            a.strava_url,
            w.temp_c_start,
            w.temp_c_max,
            w.apparent_temp_c_max,
            w.humidity_avg,
            w.precip_mm,
            w.wind_kph_avg
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        {where}
        ORDER BY a.start_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    return json.dumps(_fmt_paces([dict(r) for r in rows]))


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

    return json.dumps(_fmt_paces({
        "period": {"start": start_date, "end": end_date},
        "total": dict(totals),
        "by_type": [dict(r) for r in by_type],
    }))


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

    return json.dumps(_fmt_paces(out))


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
      laps: [{lap_index, lap_type, distance_miles, pace_min_per_mile, avg_hr, max_hr}]
    lap_type: warmup | work | recovery | float | cooldown | steady (see get_workout_session).
    Trivial laps (< 200m or < 45s) get lap_type null.
    """
    conn = _conn()
    _, sport_params = _run_type_filter()
    placeholders = ",".join("?" * len(sport_params))
    conditions = [f"a.sport_type IN ({placeholders})", "a.run_type = 'workout'"]
    params: list[str | int] = list(sport_params)
    if workout_label:
        conditions.append("a.workout_label = ?")
        params.append(workout_label)
    if name_contains:
        conditions.append("a.name LIKE ?")
        params.append(f"%{name_contains}%")
    if start_date:
        conditions.append("a.start_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("a.start_date <= ?")
        params.append(end_date)
    where = "WHERE " + " AND ".join(conditions)

    activities = conn.execute(f"""
        SELECT a.activity_id, a.name, DATE(a.start_date) AS date, a.workout_label,
               w.temp_c_start, w.temp_c_max, w.apparent_temp_c_max, w.humidity_avg, w.wind_kph_avg
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        {where}
        ORDER BY a.start_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    out = []
    for act in activities:
        laps = conn.execute("""
            SELECT
                lap_index,
                distance_m,
                moving_time_s,
                average_speed_mps,
                average_heartrate,
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

        # Classify only non-trivial laps; trivial ones get lap_type None.
        classifiable = [
            i for i, lap in enumerate(laps)
            if float(lap["distance_m"]) >= 200 and int(lap["moving_time_s"]) >= 45
            and lap["average_speed_mps"] is not None and float(lap["average_speed_mps"]) > 0
        ]
        sub_types = _classify_lap_rows([laps[i] for i in classifiable])
        lap_types: list[LapType | None] = [None] * len(laps)
        for i, t in zip(classifiable, sub_types):
            lap_types[i] = t

        keep = ("lap_index", "distance_miles", "pace_min_per_mile", "avg_hr", "max_hr")
        out.append({
            "activity_id": act["activity_id"],
            "name": act["name"],
            "date": act["date"],
            "workout_label": act["workout_label"],
            "temp_c_start": act["temp_c_start"],
            "temp_c_max": act["temp_c_max"],
            "apparent_temp_c_max": act["apparent_temp_c_max"],
            "humidity_avg": act["humidity_avg"],
            "wind_kph_avg": act["wind_kph_avg"],
            "laps": [
                {**{k: lap[k] for k in keep}, "lap_type": t}
                for lap, t in zip(laps, lap_types)
            ],
        })

    return json.dumps(_fmt_paces(out))


@mcp.tool()
def get_build_snapshot(race_date: str | None = None, build_weeks: int = 12) -> str:
    """
    Week-by-week breakdown of a marathon build.
    race_date: YYYY-MM-DD of the target race. If omitted, uses the most recent marathon in the DB.
    Returns: race info, weeks_to_race (negative = past race), week-by-week mileage with
    workout/long-run counts, all workout sessions with rep stats, and long run list.
    Use this to orient at the start of any build-specific conversation.
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()

    if race_date:
        race_date_str = race_date
        race_row = conn.execute("""
            SELECT name FROM activities
            WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
              AND DATE(start_date) = ?
        """, [MARATHON_MIN_M, MARATHON_MAX_M, race_date]).fetchone()
        race_name: str | None = race_row["name"] if race_row else None
    else:
        race_row = conn.execute("""
            SELECT name, DATE(start_date) AS race_date FROM activities
            WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
            ORDER BY start_date DESC LIMIT 1
        """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchone()
        if not race_row:
            return json.dumps({"error": "No marathon found in the database."})
        race_date_str = race_row["race_date"]
        race_name = race_row["name"]

    race_dt = date.fromisoformat(race_date_str)
    race_week_monday = race_dt - timedelta(days=race_dt.weekday())
    build_start = (race_week_monday - timedelta(weeks=build_weeks)).isoformat()
    weeks_to_race = (race_dt - date.today()).days // 7

    race_result_row = conn.execute("""
        SELECT moving_time_s, ROUND(distance_m / 1609.34, 2) AS distance_miles,
               CASE WHEN average_speed_mps > 0
                    THEN ROUND(26.8224 / average_speed_mps, 2)
                    ELSE NULL END AS pace_min_per_mile
        FROM activities
        WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
          AND DATE(start_date) = ?
    """, [MARATHON_MIN_M, MARATHON_MAX_M, race_date_str]).fetchone()
    race_result: dict[str, object] | None = dict(race_result_row) if race_result_row else None

    weeks = conn.execute(f"""
        SELECT
            CAST((julianday(DATE(start_date)) - julianday(?)) / 7.0 AS INTEGER) - ? AS week_offset,
            ROUND(SUM(distance_m) / 1609.34, 1) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN run_type = 'workout' THEN 1 ELSE 0 END) AS workouts,
            SUM(CASE WHEN run_type = 'long_run' THEN 1 ELSE 0 END) AS long_runs
        FROM activities
        WHERE {type_clause}
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        GROUP BY week_offset
        ORDER BY week_offset
    """, [build_start, build_weeks] + type_params + [build_start, race_date_str]).fetchall()

    workouts = conn.execute("""
        SELECT
            a.activity_id,
            a.name,
            DATE(a.start_date) AS date,
            a.workout_label,
            COUNT(l.lap_id) AS rep_count,
            ROUND(AVG(26.8224 / l.average_speed_mps), 2) AS avg_rep_pace,
            ROUND(AVG(l.average_heartrate), 1) AS avg_rep_hr,
            w.temp_c_start,
            w.temp_c_max,
            w.apparent_temp_c_max,
            w.humidity_avg,
            w.wind_kph_avg
        FROM activities a
        LEFT JOIN laps l ON l.activity_id = a.activity_id
            AND l.distance_m >= 200 AND l.moving_time_s >= 45
            AND l.average_speed_mps IS NOT NULL AND l.average_speed_mps > 0
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.run_type = 'workout'
          AND DATE(a.start_date) >= ? AND DATE(a.start_date) < ?
        GROUP BY a.activity_id
        ORDER BY a.start_date
    """, [build_start, race_date_str]).fetchall()

    long_runs = conn.execute(f"""
        SELECT
            DATE(a.start_date) AS date,
            ROUND(a.distance_m / 1609.34, 1) AS miles,
            CASE WHEN a.average_speed_mps > 0
                 THEN ROUND(26.8224 / a.average_speed_mps, 2)
                 ELSE NULL END AS avg_pace,
            ROUND(a.average_heartrate) AS avg_hr,
            a.activity_id,
            w.temp_c_start,
            w.temp_c_max,
            w.apparent_temp_c_max,
            w.humidity_avg,
            w.precip_mm,
            w.wind_kph_avg
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE {type_clause.replace("sport_type", "a.sport_type")} AND a.run_type = 'long_run'
          AND DATE(a.start_date) >= ? AND DATE(a.start_date) < ?
        ORDER BY a.start_date
    """, type_params + [build_start, race_date_str]).fetchall()

    return json.dumps(_fmt_paces({
        "race": race_name,
        "race_date": race_date_str,
        "race_result": race_result,
        "build_start": build_start,
        "weeks_to_race": weeks_to_race,
        "weeks": [dict(w) for w in weeks],
        "workouts": [dict(w) for w in workouts],
        "long_runs": [dict(lr) for lr in long_runs],
    }))


@mcp.tool()
def get_workout_session(activity_id: int) -> str:
    """
    Detailed view of a single workout: all laps in sequence, each classified by lap_type:
      warmup | work | recovery (jog between reps) | float (slow-but-still-work laps,
      e.g. MP flux slow halves) | cooldown | steady (no interval structure detected).
    Trivial laps (< 200m or < 45s) are filtered out.
    Each lap: lap_num, lap_type, distance_miles, duration_s, pace_min_mi, avg_hr, max_hr.
    Use this to inspect within-session structure — whether reps held even, drifted,
    or fell apart — rather than relying solely on session averages.
    activity_id comes from get_build_snapshot, compare_workouts_by_build, or get_activities.
    """
    conn = _conn()

    activity = conn.execute("""
        SELECT activity_id, name, DATE(start_date) AS date, workout_label,
               ROUND(distance_m / 1609.34, 2) AS total_miles,
               moving_time_s AS total_time_s, strava_url
        FROM activities WHERE activity_id = ?
    """, [activity_id]).fetchone()

    if not activity:
        return json.dumps({"error": f"Activity {activity_id} not found."})

    laps = conn.execute("""
        SELECT
            lap_index,
            distance_m,
            average_speed_mps,
            average_heartrate,
            ROUND(distance_m / 1609.34, 3) AS distance_miles,
            moving_time_s AS duration_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_mi,
            ROUND(average_heartrate) AS avg_hr,
            ROUND(max_heartrate) AS max_hr
        FROM laps
        WHERE activity_id = ?
          AND distance_m >= 200 AND moving_time_s >= 45
          AND average_speed_mps IS NOT NULL AND average_speed_mps > 0
        ORDER BY lap_index
    """, [activity_id]).fetchall()

    lap_types = _classify_lap_rows(laps)
    keep = ("lap_index", "distance_miles", "duration_s", "pace_min_mi", "avg_hr", "max_hr")
    return json.dumps(_fmt_paces({
        **dict(activity),
        "laps": [
            {"lap_num": i + 1, "lap_type": t, **{k: r[k] for k in keep}}
            for i, (r, t) in enumerate(zip(laps, lap_types))
        ],
    }))


@mcp.tool()
def get_easy_hr_trend(months: int = 36) -> str:
    """
    Monthly average HR and pace for easy runs — the primary long-term aerobic fitness signal.
    A declining HR trend at stable or faster paces indicates improving aerobic efficiency
    accumulated across builds, not attributable to any single cycle.
    Returns months with avg_hr, avg_pace_min_mi, run_count. Filtered to easy-tagged runs only.
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()

    cutoff_dt = date.today() - timedelta(days=months * 30)
    cutoff = cutoff_dt.isoformat()

    rows = conn.execute(f"""
        SELECT
            strftime('%Y-%m', start_date) AS month,
            COUNT(*) AS runs,
            ROUND(AVG(average_heartrate), 1) AS avg_hr,
            CASE WHEN AVG(average_speed_mps) > 0
                 THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                 ELSE NULL END AS avg_pace_min_mi
        FROM activities
        WHERE {type_clause}
          AND run_type = 'easy'
          AND average_heartrate IS NOT NULL
          AND start_date >= ?
        GROUP BY month
        ORDER BY month
    """, type_params + [cutoff]).fetchall()

    return json.dumps(_fmt_paces([dict(r) for r in rows]))


@mcp.tool()
def compare_workouts_by_build(
    workout_label: str,
    build_weeks: int = 12,
) -> str:
    """
    Compare workout sessions (by label) across marathon builds.
    Laps are classified (warmup/work/recovery/float/cooldown) and stats reflect
    work laps only — warmup miles, jog recoveries, and cooldowns are excluded.
    For flux-style sessions both alternating halves count as work.
    Returns builds chronologically, each with per-session:
      date, name, rep_count, avg_rep_pace_min_mi, avg_rep_hr, best_rep_pace_min_mi
    Use this for cross-build quality questions: "Did my LT pace drop at lower HR over time?"
    Drill into a single session's lap-by-lap structure with get_workout_session.
    """
    conn = _conn()

    races = conn.execute("""
        SELECT name, DATE(start_date) AS race_date
        FROM activities
        WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
        ORDER BY race_date
    """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchall()

    # Fetch session metadata and all non-trivial laps separately, then filter in Python.
    session_rows = conn.execute("""
        SELECT a.activity_id, a.name, DATE(a.start_date) AS date,
               w.temp_c_start, w.temp_c_max, w.apparent_temp_c_max, w.humidity_avg, w.wind_kph_avg
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.workout_label = ? AND a.run_type = 'workout'
        ORDER BY a.start_date
    """, [workout_label]).fetchall()

    if not session_rows:
        return json.dumps([])

    id_list = [int(row["activity_id"]) for row in session_rows]
    placeholders = ",".join("?" * len(id_list))
    lap_rows = conn.execute(f"""
        SELECT activity_id, distance_m, moving_time_s, average_speed_mps, average_heartrate
        FROM laps
        WHERE activity_id IN ({placeholders})
          AND distance_m >= 200 AND moving_time_s >= 45
          AND average_speed_mps IS NOT NULL AND average_speed_mps > 0
        ORDER BY activity_id, lap_index
    """, id_list).fetchall()

    # Group laps by activity
    laps_by_id: dict[int, list[sqlite3.Row]] = {aid: [] for aid in id_list}
    for lap in lap_rows:
        laps_by_id[int(lap["activity_id"])].append(lap)

    # Compute per-session rep stats over classified work laps
    session_stats: dict[int, dict[str, object]] = {}
    for activity_id, laps in laps_by_id.items():
        lap_types = _classify_lap_rows(laps)
        rep_laps = [lap for lap, t in zip(laps, lap_types) if t == "work"]
        if not rep_laps:
            continue
        paces = [26.8224 / float(l["average_speed_mps"]) for l in rep_laps]
        hrs = [float(l["average_heartrate"]) for l in rep_laps if l["average_heartrate"] is not None]
        session_stats[activity_id] = {
            "rep_count": len(rep_laps),
            "avg_rep_pace": round(sum(paces) / len(paces), 2),
            "avg_rep_hr": round(sum(hrs) / len(hrs), 1) if hrs else None,
            "best_rep_pace": round(min(paces), 2),
        }

    sessions_by_id = {int(row["activity_id"]): row for row in session_rows}

    builds: list[dict[str, object]] = []
    for race in races:
        race_date_str: str = race["race_date"]
        race_dt = date.fromisoformat(race_date_str)
        race_week_monday = race_dt - timedelta(days=race_dt.weekday())
        build_start = (race_week_monday - timedelta(weeks=build_weeks)).isoformat()

        build_sessions = []
        for aid in id_list:
            row = sessions_by_id[aid]
            if build_start <= str(row["date"]) < race_date_str and aid in session_stats:
                build_sessions.append({
                    "activity_id": aid,
                    "name": row["name"],
                    "date": row["date"],
                    **session_stats[aid],
                    "temp_c_start": row["temp_c_start"],
                    "temp_c_max": row["temp_c_max"],
                    "apparent_temp_c_max": row["apparent_temp_c_max"],
                    "humidity_avg": row["humidity_avg"],
                    "wind_kph_avg": row["wind_kph_avg"],
                })

        if build_sessions:
            builds.append({
                "race": race["name"],
                "race_date": race_date_str,
                "sessions": build_sessions,
            })

    return json.dumps(_fmt_paces(builds))


@mcp.tool()
def get_activity_weather(activity_id: int) -> str:
    """
    Hourly weather breakdown for a specific activity.
    Returns temp, apparent (feels-like) temp, humidity, wind, and precipitation
    for each hour of the run — useful when understanding how conditions evolved
    during a long run (e.g. started cool but got hot by mile 18).
    Also returns summary stats: temp at start/end/max, avg humidity, total precip.
    Returns null hourly field if weather hasn't been synced for this activity (run miles-sync).
    activity_id comes from get_activities or get_build_snapshot.
    """
    conn = _conn()
    row = conn.execute("""
        SELECT
            a.activity_id, a.name, DATE(a.start_date) AS date, a.run_type,
            ROUND(a.distance_m / 1609.34, 2) AS miles, a.moving_time_s,
            w.temp_c_start, w.temp_c_end, w.temp_c_avg, w.temp_c_max,
            w.apparent_temp_c_max, w.humidity_avg, w.precip_mm, w.wind_kph_avg,
            w.hourly_json
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.activity_id = ?
    """, [activity_id]).fetchone()

    if not row:
        return json.dumps({"error": f"Activity {activity_id} not found."})

    result = dict(row)
    hourly_raw = result.pop("hourly_json", None)
    result["hourly"] = json.loads(hourly_raw) if hourly_raw else None
    return json.dumps(result)


@mcp.tool()
def run_sql(query: str) -> str:
    """
    Run a read-only SQL SELECT against the database.
    Use this for ad-hoc questions the other tools don't cover.

    Table: activities
      activity_id, name, sport_type, start_date, workout_type, run_type, workout_label,
      distance_m, moving_time_s, elapsed_time_s, total_elevation_gain_m,
      average_speed_mps, max_speed_mps, average_heartrate, max_heartrate,
      average_cadence, gear_id, strava_url, synced_at, start_lat, start_lng

    Table: laps  (one row per lap; only workout activities are synced)
      lap_id, activity_id, lap_index, distance_m, moving_time_s, average_speed_mps,
      average_heartrate, max_heartrate, average_cadence, total_elevation_gain_m, pace_zone

    Table: weather  (one row per activity; populated by miles-sync)
      activity_id, fetched_at, temp_c_start, temp_c_end, temp_c_avg, temp_c_max,
      apparent_temp_c_max, humidity_avg, precip_mm, wind_kph_avg, hourly_json
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
