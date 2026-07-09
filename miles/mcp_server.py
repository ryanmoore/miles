import json
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from typing import Literal, Sequence, cast

from mcp.server.fastmcp import FastMCP

from . import db
from .builds import Build, RaceRef, detect_builds
from .derive import derive_all, ensure_derived
from .fitness import WINDOW_DAYS, estimate_fitness
from .format import fmt_pace, fmt_time
from .periods import GAP_WEEKS_TO_SPLIT, Period, WeekAgg, is_active, sunday_of, detect_periods
from .plan import (
    DayInput,
    PlanValidationError,
    WeekInput,
    add_log_entry,
    commit_plan as _plan_commit_plan,
    current_version_for_week,
    delete_draft_days as _plan_delete_draft_days,
    delete_draft_weeks as _plan_delete_draft_weeks,
    diff_versions,
    discard_draft as _plan_discard_draft,
    get_active_plan,
    get_draft as _plan_get_draft,
    get_version,
    start_plan_draft as _plan_start_plan_draft,
    start_revision_draft as _plan_start_revision_draft,
    upsert_draft_days,
    upsert_draft_weeks,
)
from .races import (
    MARATHON_MAX_M,
    MARATHON_MIN_M,
    NOMINAL_METERS,
    classify_race_distance,
    race_rows,
    riegel_time,
)

# stateless_http applies only to the streamable-HTTP transport that api.py
# mounts at /mcp (stdio via `miles-mcp` is unaffected): every request is
# self-contained, so a server restart or dev reload never strands a client
# holding a dead session id. Nothing here needs cross-request server state.
mcp = FastMCP("miles", stateless_http=True)

RUN_TYPES = ("Run", "TrailRun", "VirtualRun")


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    ensure_derived(conn)
    return conn


_PACE_KEYS = frozenset({
    "pace_min_per_mile", "avg_pace_min_per_mile", "avg_pace",
    "avg_rep_pace", "best_rep_pace", "pace_min_mi", "avg_pace_min_mi",
    "first_half_pace", "second_half_pace", "pace_lo", "pace_hi",
})


def _fmt_paces(obj: object) -> object:
    """Recursively convert known pace keys from decimal float to MM:SS string."""
    if isinstance(obj, dict):
        return {
            k: (fmt_pace(float(v)) if k in _PACE_KEYS and isinstance(v, (int, float)) else _fmt_paces(v))
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
    run_type: 'easy' | 'workout' | 'long_run' | 'race' | None for all. Matched against the
    effective run type — the athlete's explicit Strava tag when set, else an inferred type
    for untagged (workout_type=0) activities (see run_type_source in each row).
    Dates are YYYY-MM-DD. Returns up to `limit` rows, newest first.
    Weather fields (temp_c_start, temp_c_max, apparent_temp_c_max, humidity_avg,
    precip_mm, wind_kph_avg) are null if not yet synced for that activity.
    """
    conn = _conn()
    effective_run_type = db.effective_run_type_sql("a")
    _, sport_params = _run_type_filter()
    placeholders = ",".join("?" * len(sport_params))
    conditions = [f"a.sport_type IN ({placeholders})"]
    params: list[str | int] = list(sport_params)
    if run_type:
        conditions.append(f"{effective_run_type} = ?")
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
            {effective_run_type} AS run_type,
            CASE WHEN a.workout_type = 0 AND a.run_type_inferred IS NOT NULL
                 THEN 'inferred' ELSE 'strava' END AS run_type_source,
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
    effective_run_type = db.effective_run_type_sql()
    type_clause, base_params = _run_type_filter()
    date_clause = "AND start_date >= ? AND start_date <= ?"
    date_params = [start_date, end_date]

    by_type = conn.execute(f"""
        SELECT
            {effective_run_type} AS run_type,
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
        GROUP BY {effective_run_type}
        ORDER BY {effective_run_type}
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

    For non-marathon races or effective-type awareness, see get_race_comparison.
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
def get_race_comparison(distance_category: str | None = None, build_weeks: int = 12) -> str:
    """
    Cross-race comparison of pre-race training windows — get_marathon_comparison's shape,
    generalized to any race distance. For every effective-race activity (optionally
    filtered to one distance_category: 5K, 10K, half, marathon, ...), returns the result
    alongside stats for the build_weeks-week window that preceded it. Ascending by date.

    Each entry: name, date, distance_category, run_type_source, finish_time_s,
    distance_miles, pace_min_per_mile,
    build: { start, end, weeks, total_miles, avg_mpw,
             by_type: { easy, workout, long_run, race? } } (by_type keyed on the
    effective run type, so untagged rows still bucket correctly),
    window_coverage: { weeks_total, active_weeks, period, detected_build } — same shape
    and caveats as get_build_snapshot's: active_weeks well below weeks_total means the
    fixed window wasn't a real build.

    Deltas across distance_category are not comparable times — a faster half than 10K
    build says nothing about fitness change. Use get_race_equivalents or the
    personal-best tools for cross-distance comparison, not this tool's raw numbers.

    Returns [] if no races found.
    """
    conn = _conn()
    effective_run_type = db.effective_run_type_sql()
    type_clause, type_params = _run_type_filter()

    races = conn.execute(f"""
        SELECT
            name,
            DATE(start_date) AS race_date,
            distance_m,
            ROUND(distance_m / 1609.34, 2) AS distance_miles,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            CASE WHEN workout_type = 0 AND run_type_inferred IS NOT NULL
                 THEN 'inferred' ELSE 'strava' END AS run_type_source
        FROM activities
        WHERE {type_clause} AND {effective_run_type} = 'race'
        ORDER BY race_date
    """, type_params).fetchall()

    periods, builds = _full_periods_and_builds(conn)

    out = []
    for race in races:
        category = classify_race_distance(race["distance_m"]) or "other"
        if distance_category is not None and category != distance_category:
            continue

        race_date: str = race["race_date"]
        # Matches get_marathon_comparison's day-based window exactly (not Monday-aligned)
        # so the two tools' build stats agree; window_coverage below stays Monday-aligned
        # like the rest of the codebase.
        build_start: str = conn.execute(
            "SELECT DATE(?, ?)", (race_date, f"-{build_weeks * 7} days")
        ).fetchone()[0]

        by_type = conn.execute(f"""
            SELECT
                {effective_run_type} AS run_type,
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
            GROUP BY {effective_run_type}
            ORDER BY {effective_run_type}
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

        race_dt = date.fromisoformat(race_date)
        race_week_monday = race_dt - timedelta(days=race_dt.weekday())
        window_coverage = _window_coverage(conn, race_date, race_week_monday, build_weeks, periods, builds)

        out.append({
            "name": race["name"],
            "date": race_date,
            "distance_category": category,
            "run_type_source": race["run_type_source"],
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
            "window_coverage": window_coverage,
        })

    return json.dumps(_fmt_paces(out))


@mcp.tool()
def get_race_history(distance_category: str | None = None, start_date: str | None = None) -> str:
    """
    Every race at every distance, with PR flags and an 8-week pre-race training
    snapshot — the casual/varied-distance athlete's analogue of
    get_marathon_comparison.

    distance_category comes from distance buckets (5K, 10K, half, marathon, ...);
    non-standard distances fall into "other" and are never PR-flagged.
    is_pr means "was a PR when run": the fastest finish so far, chronologically,
    within its category — a later faster race supersedes it for later dates, but
    the historical flag on the earlier race stays true.

    Optionally filter by distance_category (applied after categorization, so
    "other" is filterable too) or start_date (YYYY-MM-DD, inclusive) — filtering
    never changes is_pr, which always reflects full race history.
    Results are ascending by date (newest last). Returns [] if no races found.

    Each race also carries pre_race_8wk: runs, miles, longest_run_miles, and
    active_weeks over the 8 Monday-aligned weeks before the race's week
    (excluding race week itself) — active week defined as in get_training_periods.

    Each race also carries effort (raced/hard/casual, null when unclassified —
    no checkpoint prediction was available) and effort_ratio (actual/predicted
    pace at the time, >1 = slower): how hard the race was actually run judged
    against the athlete's estimated fitness then plus HR, not just its raw pace.
    A hard-day race can legitimately read "hard" even when genuinely raced;
    treat a ratio near a band edge as a close call, not a hard verdict.
    """
    conn = _conn()
    rows = race_rows(conn, distance_category=distance_category, start_date=start_date)

    type_clause, type_params = _run_type_filter()
    for row in rows:
        race_dt = date.fromisoformat(str(row["date"]))
        race_week_monday = race_dt - timedelta(days=race_dt.weekday())
        window_start = race_week_monday - timedelta(weeks=8)

        week_rows = conn.execute(f"""
            SELECT
                DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
                ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
                COUNT(*) AS runs
            FROM activities
            WHERE {type_clause}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
            GROUP BY monday
        """, type_params + [window_start.isoformat(), race_week_monday.isoformat()]).fetchall()

        by_monday = {r["monday"]: r for r in week_rows}
        active_weeks = 0
        d = window_start
        while d < race_week_monday:
            wr = by_monday.get(d.isoformat())
            week: WeekAgg = {
                "monday": d.isoformat(),
                "miles": (wr["miles"] or 0.0) if wr else 0.0,
                "runs": (wr["runs"] or 0) if wr else 0,
                "workouts": 0,
            }
            if is_active(week):
                active_weeks += 1
            d += timedelta(weeks=1)

        totals = conn.execute(f"""
            SELECT
                COUNT(*) AS runs,
                ROUND(SUM(distance_m) / 1609.34, 1) AS miles,
                ROUND(MAX(distance_m) / 1609.34, 1) AS longest_run_miles
            FROM activities
            WHERE {type_clause}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
        """, type_params + [window_start.isoformat(), race_week_monday.isoformat()]).fetchone()

        row["pre_race_8wk"] = {
            "runs": totals["runs"] or 0,
            "miles": totals["miles"] or 0.0,
            "longest_run_miles": totals["longest_run_miles"] or 0.0,
            "active_weeks": active_weeks,
        }

    return json.dumps(_fmt_paces(rows))


@mcp.tool()
def get_personal_bests() -> str:
    """
    Personal bests per race distance category, each with its full
    chronological progression — the direct answer to "what are my PRs?"

    Each entry: category, nominal_miles, best ({date, name, finish_time,
    finish_time_s, pace_min_per_mile, activity_id, strava_url}), attempts
    (race count in that category), and progression — every race in the
    category in date order, each {date, name, finish_time, effort,
    delta_s_vs_prior_best}. delta_s_vs_prior_best is vs. the best time set
    *before* that race: negative means that race set a new PR (seconds
    faster), positive means seconds behind the standing PR, null for the
    first race in a category. "other"-category races and races with no
    recorded finish time are excluded entirely (missing from attempts too).
    Sorted by nominal distance ascending. Returns [] if no races.

    Caveat: PR math counts every race the same regardless of effort — a
    casual-effort race (a 5K jogged with friends) still counts toward a PR
    the same as an all-out one. effort (raced/hard/casual, null when
    unclassified) is included per progression entry so a suspiciously
    fast or slow PR can be cross-checked before taking it at face value.
    """
    conn = _conn()
    rows = race_rows(conn)

    by_category: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        if r["distance_category"] == "other" or r["finish_time_s"] is None:
            continue
        category = str(r["distance_category"])
        by_category.setdefault(category, []).append(r)

    out: list[dict[str, object]] = []
    for category in sorted(by_category, key=lambda c: NOMINAL_METERS.get(c, float("inf"))):
        races = by_category[category]  # already ascending by date via race_rows
        progression: list[dict[str, object]] = []
        prior_best_s: int | None = None
        best_row: dict[str, object] | None = None
        for r in races:
            finish_time_s = r["finish_time_s"]
            assert isinstance(finish_time_s, int)
            delta = None if prior_best_s is None else finish_time_s - prior_best_s
            progression.append({
                "date": r["date"],
                "name": r["name"],
                "finish_time": r["finish_time"],
                "effort": r["effort"],
                "delta_s_vs_prior_best": delta,
            })
            if prior_best_s is None or finish_time_s < prior_best_s:
                prior_best_s = finish_time_s
                best_row = r

        assert best_row is not None
        out.append({
            "category": category,
            "nominal_miles": round(NOMINAL_METERS[category] / 1609.34, 2),
            "best": {
                "date": best_row["date"],
                "name": best_row["name"],
                "finish_time": best_row["finish_time"],
                "finish_time_s": best_row["finish_time_s"],
                "pace_min_per_mile": best_row["pace_min_per_mile"],
                "activity_id": best_row["activity_id"],
                "strava_url": best_row["strava_url"],
            },
            "attempts": len(races),
            "progression": progression,
        })

    return json.dumps(_fmt_paces(out))


_EQUIV_CATEGORIES: tuple[str, ...] = ("5K", "10K", "half", "marathon")


@mcp.tool()
def get_race_equivalents(exponent: float = 1.06, include_casual: bool = False) -> str:
    """
    Cross-distance comparison via Riegel scaling: every race's actual result
    plus predicted-equivalent times at 5K/10K/half/marathon, so "was my recent
    10K better than last year's half?" has a common answer. `marathon_equiv_s`
    is the ranking key. Races are grouped by year, best marathon-equivalent
    first within each year; `best_ever` is the single best entry overall.
    "other"-category races and races with no recorded finish time are excluded.

    By default, races classified race_effort='casual' (a 5K jogged with
    friends) are excluded from rankings and best_ever — a casual race isn't a
    fitness data point. Pass include_casual=True to see everything, e.g. to
    audit what got excluded. Unclassified races (no checkpoint prediction
    available) are always included.

    Caveats the analyst must relay whenever citing this: Riegel assumes
    equivalent training specificity across distances — a marathon-trained
    runner's 5K equivalent may be soft, and vice versa. Recreational athletes
    typically underperform the prediction as distance goes up (endurance,
    not just fitness, gates the longer distances). The exponent is a tunable
    knob, not a physical constant — treat its output as an estimate, not
    truth. A race classified "hard" rather than "raced" may still have been a
    genuine max effort on a bad day — cross-check a suspicious ranking
    against the race's actual pace and effort before citing it.
    """
    conn = _conn()
    rows = race_rows(conn)

    entries: list[tuple[float, dict[str, object]]] = []
    for r in rows:
        category = str(r["distance_category"])
        finish_time_s = r["finish_time_s"]
        if category == "other" or not isinstance(finish_time_s, (int, float)):
            continue
        if not include_casual and r["effort"] == "casual":
            continue
        from_m = NOMINAL_METERS[category]

        equivalents: dict[str, object] = {}
        marathon_equiv_s = 0.0
        for target in _EQUIV_CATEGORIES:
            to_m = NOMINAL_METERS[target]
            t_s = riegel_time(float(finish_time_s), from_m, to_m, exponent)
            miles = to_m / 1609.34
            equivalents[target] = {
                "time_s": round(t_s, 1),
                "time": fmt_time(round(t_s)),
                "pace_min_per_mile": (t_s / 60) / miles,
            }
            if target == "marathon":
                marathon_equiv_s = t_s

        entries.append((marathon_equiv_s, {
            "date": r["date"],
            "name": r["name"],
            "distance_category": category,
            "finish_time": r["finish_time"],
            "marathon_equiv": fmt_time(round(marathon_equiv_s)),
            "marathon_equiv_s": round(marathon_equiv_s, 1),
            "equivalents": equivalents,
            "is_pr": r["is_pr"],
            "run_type_source": r["run_type_source"],
            "effort": r["effort"],
        }))

    races_by_year: dict[str, list[tuple[float, dict[str, object]]]] = {}
    for key, e in entries:
        year = str(e["date"])[:4]
        races_by_year.setdefault(year, []).append((key, e))

    out_by_year: dict[str, list[dict[str, object]]] = {
        year: [e for _, e in sorted(pairs, key=lambda p: p[0])]
        for year, pairs in races_by_year.items()
    }
    best_ever = min(entries, key=lambda p: p[0])[1] if entries else None

    return json.dumps(_fmt_paces({
        "races": out_by_year,
        "best_ever": best_ever,
        "exponent": exponent,
    }))


@mcp.tool()
def get_training_periods(start_date: str | None = None) -> str:
    """
    Detects stretches of consistent training ("periods") separated by gaps of
    3+ empty weeks, from weekly mileage/run-count. Use this instead of assuming
    a fixed build window when training is sporadic — a sporadic athlete's real
    structure is bursts of training separated by gaps, not clean 12-week builds.

    Periods describe continuity, not race preparation: there is no maximum
    period length, so a consistent year-round runner correctly yields one long
    period — that is not a "build" and should not be described as one.
    `fragment: true` marks short-lived active clusters too brief to call a period.

    Optionally restrict to weeks starting on/after start_date (YYYY-MM-DD).
    Returns {"periods": [...], "gaps": [{"start", "end", "weeks"}, ...]}.
    Each period also carries `races`: races (effective run type, including
    inferred) that fall within it, each as
    {date, name, distance_category, distance_miles}.

    Each period also carries `builds`: race-anchored preparation windows
    within it, distinct from the period itself. A build is capped at 18
    weeks and trimmed back to where training volume actually ramped up, so
    it never balloons into the whole period. Only races 10K and up anchor a
    build (shorter races still appear in `races` but anchor nothing).
    `bounded_by` says why each build starts where it does: `cap` (hit the
    18-week ceiling), `prior_race` (too close to a previous anchor race),
    `period_start` (the period itself is shorter than the cap), or `ramp`
    (volume before that point was too low to count as preparation).
    `thin: true` flags a build under 4 weeks. For a consistent, year-round
    runner the enclosing period is just continuity — the builds within it
    are the meaningful analysis windows for "what led into this race."
    """
    conn = _conn()
    effective_run_type = db.effective_run_type_sql()
    type_clause, type_params = _run_type_filter()
    where = f"WHERE {type_clause}"
    params = list(type_params)
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)

    week_rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN {effective_run_type} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        {where}
        GROUP BY monday
        ORDER BY monday
    """, params).fetchall()

    weeks: list[WeekAgg] = [
        {"monday": r["monday"], "miles": r["miles"] or 0.0, "runs": r["runs"], "workouts": r["workouts"] or 0}
        for r in week_rows
    ]
    periods, gaps = detect_periods(weeks)
    if not periods:
        return json.dumps({"periods": [], "gaps": []})

    race_type_clause, race_type_params = _run_type_filter()
    race_activity_rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, name, distance_m,
               ROUND(distance_m / 1609.34, 2) AS distance_miles
        FROM activities
        WHERE {race_type_clause} AND {effective_run_type} = 'race'
        ORDER BY date
    """, race_type_params).fetchall()

    races = [
        {
            "date": r["date"],
            "name": r["name"],
            "distance_category": classify_race_distance(r["distance_m"]) or "other",
            "distance_miles": r["distance_miles"],
        }
        for r in race_activity_rows
    ]
    race_refs: list[RaceRef] = [
        {
            "date": r["date"],
            "name": r["name"],
            "distance_category": classify_race_distance(r["distance_m"]) or "other",
            "distance_m": r["distance_m"],
        }
        for r in race_activity_rows
        if r["distance_m"] is not None
    ]
    builds = detect_builds(weeks, race_refs, periods)

    out_periods = [
        {
            **p,
            "races": [r for r in races if p["start"] <= r["date"] <= p["end"]],
            "builds": [b for b in builds if p["start"] <= b["race"]["date"] <= p["end"]],
        }
        for p in periods
    ]
    return json.dumps({"periods": out_periods, "gaps": gaps})


def _longest_inactive_run(weeks: list[WeekAgg]) -> tuple[int, int] | None:
    """Longest run of consecutive inactive weeks as (start_index, length); None if none."""
    best: tuple[int, int] | None = None
    run_start = 0
    run_len = 0
    for i, week in enumerate(weeks):
        if is_active(week):
            run_len = 0
            continue
        if run_len == 0:
            run_start = i
        run_len += 1
        if best is None or run_len > best[1]:
            best = (run_start, run_len)
    return best


def _months_between(start: date, end: date) -> list[str]:
    """Inclusive 'YYYY-MM' strings from start's month through end's month."""
    months: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


@mcp.tool()
def get_consistency_report(months: int = 12) -> str:
    """
    The headline tool for "how consistent has my running been?" — streaks, gaps,
    and ramp in volume over a trailing window (months x 30 days). For a sporadic
    athlete this is the primary lens: lead with streaks/gaps/ramp here rather
    than build language. get_training_periods and get_build_snapshot assume more
    continuous training and describe preparation, not day-to-day consistency;
    this tool doesn't attach races or builds — use get_training_periods for that.

    Returns {"error": "No runs found."} if there are no runs in the window at all.
    Otherwise, a JSON object:
      last_run: {date, days_ago} — most recent run in the window.
      current_streak_weeks: consecutive active weeks counting back from the
        current (possibly partial) week. If the current week isn't active yet,
        it's skipped rather than treated as a streak-breaker, and counting
        starts from last week instead.
      current_gap_weeks: present only when current_streak_weeks is 0 —
        consecutive inactive weeks ending at last week.
      longest_gap: {start, end, weeks} — longest run of consecutive inactive
        weeks inside the window; null if the window has no inactive week.
      rolling: last_4wk vs prior_4wk ({miles, runs}, last 28 vs prior 28 days),
        plus ramp_pct (percent change in miles; null when prior_4wk has zero
        miles, since percent change off zero is undefined rather than 0).
        `note` is added only when the ramp is large — either ramp_pct > 30, or
        prior_4wk was completely inactive (an unmeasurable but clearly large
        ramp) — *and* the 8 weeks before the last-4wk window contain a gap of
        3+ consecutive inactive weeks. The note is phrased descriptively
        ("volume is ramping quickly off a break"); relay it as-is, don't turn
        it into medical or injury-risk advice.
      monthly: one entry per calendar month intersecting the window, ascending
        — {month: 'YYYY-MM', runs, miles, longest_run_miles, active_weeks}.
        active_weeks counts Monday-aligned weeks whose Monday falls in that
        month (so a week can only ever count toward one month).
      periods: detect_periods (periods.py) run over just this window's weeks,
        fragments included — continuity only, no races/builds attached.

    An active week clears periods.ACTIVE_WEEK_MIN_RUNS runs or
    periods.ACTIVE_WEEK_MIN_MILES miles (periods.is_active).
    """
    conn = _conn()
    effective_run_type = db.effective_run_type_sql()
    type_clause, type_params = _run_type_filter()

    today = date.today()
    window_start = today - timedelta(days=months * 30)

    last_row = conn.execute(f"""
        SELECT DATE(start_date) AS date FROM activities
        WHERE {type_clause} AND DATE(start_date) >= ?
        ORDER BY start_date DESC LIMIT 1
    """, type_params + [window_start.isoformat()]).fetchone()
    if last_row is None:
        return json.dumps({"error": "No runs found."})
    last_run_date = date.fromisoformat(last_row["date"])

    # Monday-aligned weekly aggregates, zero-filled from the window start's week
    # through the current week (not just through the last activity), so a
    # trailing gap up to today shows up rather than being silently dropped.
    week_rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN {effective_run_type} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        WHERE {type_clause} AND DATE(start_date) >= ?
        GROUP BY monday
        ORDER BY monday
    """, type_params + [window_start.isoformat()]).fetchall()
    by_monday = {r["monday"]: r for r in week_rows}

    first_monday = window_start - timedelta(days=window_start.weekday())
    this_week_monday = today - timedelta(days=today.weekday())
    weeks: list[WeekAgg] = []
    d = first_monday
    while d <= this_week_monday:
        iso = d.isoformat()
        r = by_monday.get(iso)
        weeks.append({
            "monday": iso,
            "miles": (r["miles"] or 0.0) if r else 0.0,
            "runs": (r["runs"] or 0) if r else 0,
            "workouts": (r["workouts"] or 0) if r else 0,
        })
        d += timedelta(weeks=1)

    # Streak: count back from the current week; if it's not active yet, skip
    # it (not a streak-breaker) and start counting from last week instead.
    idx = len(weeks) - 1
    if not is_active(weeks[idx]):
        idx -= 1
    streak = 0
    j = idx
    while j >= 0 and is_active(weeks[j]):
        streak += 1
        j -= 1

    result: dict[str, object] = {
        "last_run": {"date": last_run_date.isoformat(), "days_ago": (today - last_run_date).days},
        "current_streak_weeks": streak,
    }
    if streak == 0:
        gap = 0
        k = idx
        while k >= 0 and not is_active(weeks[k]):
            gap += 1
            k -= 1
        result["current_gap_weeks"] = gap

    inactive_run = _longest_inactive_run(weeks)
    if inactive_run is None:
        result["longest_gap"] = None
    else:
        start_i, length = inactive_run
        result["longest_gap"] = {
            "start": weeks[start_i]["monday"],
            "end": sunday_of(weeks[start_i + length - 1]["monday"]),
            "weeks": length,
        }

    # Rolling 4-week ramp: last 28 calendar days vs. the prior 28.
    last_start = today - timedelta(days=27)
    prior_end = last_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=27)

    def _range_stats(start: date, end: date) -> tuple[float, int]:
        row = conn.execute(f"""
            SELECT ROUND(SUM(distance_m) / 1609.34, 2) AS miles, COUNT(*) AS runs
            FROM activities
            WHERE {type_clause} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        """, type_params + [start.isoformat(), end.isoformat()]).fetchone()
        return (row["miles"] or 0.0), (row["runs"] or 0)

    last_miles, last_runs = _range_stats(last_start, today)
    prior_miles, prior_runs = _range_stats(prior_start, prior_end)
    ramp_pct = None if prior_miles == 0 else round((last_miles - prior_miles) / prior_miles * 100, 1)

    rolling: dict[str, object] = {
        "last_4wk": {"miles": last_miles, "runs": last_runs},
        "prior_4wk": {"miles": prior_miles, "runs": prior_runs},
        "ramp_pct": ramp_pct,
    }
    # A completely inactive prior_4wk is an unmeasurable (undefined) ramp_pct,
    # not a non-ramp -- treat it as large for the note, same as ramp_pct > 30.
    is_large_ramp = (ramp_pct is not None and ramp_pct > 30) or (prior_miles == 0 and last_miles > 0)
    if is_large_ramp:
        lookback_end = last_start - timedelta(days=last_start.weekday())
        lookback_start = lookback_end - timedelta(weeks=8)
        lookback_weeks = [
            w for w in weeks
            if lookback_start.isoformat() <= w["monday"] < lookback_end.isoformat()
        ]
        lookback_gap = _longest_inactive_run(lookback_weeks)
        if lookback_gap is not None and lookback_gap[1] >= GAP_WEEKS_TO_SPLIT:
            rolling["note"] = "Volume is ramping quickly off a break in training."
    result["rolling"] = rolling

    month_rows = conn.execute(f"""
        SELECT strftime('%Y-%m', start_date) AS month,
               COUNT(*) AS runs,
               ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
               ROUND(MAX(distance_m) / 1609.34, 2) AS longest_run_miles
        FROM activities
        WHERE {type_clause} AND DATE(start_date) >= ?
        GROUP BY month
        ORDER BY month
    """, type_params + [window_start.isoformat()]).fetchall()
    month_data = {r["month"]: r for r in month_rows}

    active_weeks_by_month: dict[str, int] = {}
    for w in weeks:
        if is_active(w):
            month = w["monday"][:7]
            active_weeks_by_month[month] = active_weeks_by_month.get(month, 0) + 1

    monthly: list[dict[str, object]] = []
    for month in _months_between(window_start, today):
        r = month_data.get(month)
        monthly.append({
            "month": month,
            "runs": r["runs"] if r else 0,
            "miles": (r["miles"] or 0.0) if r else 0.0,
            "longest_run_miles": (r["longest_run_miles"] or 0.0) if r else 0.0,
            "active_weeks": active_weeks_by_month.get(month, 0),
        })
    result["monthly"] = monthly

    window_periods, _window_gaps = detect_periods(weeks)
    result["periods"] = window_periods

    return json.dumps(_fmt_paces(result))


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
      activity_id, name, date, workout_label, dominant_intensity,
      laps: [{lap_index, lap_type, intensity, distance_miles, pace_min_per_mile, avg_hr, max_hr}]
    lap_type: warmup | work | recovery | float | cooldown | steady (see get_workout_session).
    Trivial laps (< 200m or < 45s) get lap_type null.
    intensity: what a work/float lap was run at relative to the athlete's estimated
    fitness at the time — MP | threshold | interval | repetition | aerobic | sprint |
    sub-<zone>, or null for a non-work lap, a trivial lap, or a month with no reliable
    fitness estimate. dominant_intensity is the session-level rollup (the intensity
    holding >=60% of work-lap time); null when mixed or unavailable. Both stand in for
    workout_label on unlabeled sessions — see run_sql for querying by dominant_intensity.
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
               a.dominant_intensity,
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
                lap_type,
                intensity,
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

        keep = ("lap_index", "distance_miles", "pace_min_per_mile", "avg_hr", "max_hr", "lap_type", "intensity")
        out.append({
            "activity_id": act["activity_id"],
            "name": act["name"],
            "date": act["date"],
            "workout_label": act["workout_label"],
            "dominant_intensity": act["dominant_intensity"],
            "temp_c_start": act["temp_c_start"],
            "temp_c_max": act["temp_c_max"],
            "apparent_temp_c_max": act["apparent_temp_c_max"],
            "humidity_avg": act["humidity_avg"],
            "wind_kph_avg": act["wind_kph_avg"],
            "laps": [{k: lap[k] for k in keep} for lap in laps],
        })

    return json.dumps(_fmt_paces(out))


def _full_periods_and_builds(conn: sqlite3.Connection) -> tuple[list[Period], list[Build]]:
    """
    Full-history weekly aggregates, detected periods, and race-anchored builds — the
    same computation get_training_periods exposes over all effective-race activities,
    reused here to locate the period/build enclosing an arbitrary race date. No
    start_date filter: the enclosing period must be found from the whole history.
    """
    effective_run_type = db.effective_run_type_sql()
    type_clause, type_params = _run_type_filter()

    week_rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN {effective_run_type} = 'workout' THEN 1 ELSE 0 END) AS workouts
        FROM activities
        WHERE {type_clause}
        GROUP BY monday
        ORDER BY monday
    """, type_params).fetchall()

    weeks: list[WeekAgg] = [
        {"monday": r["monday"], "miles": r["miles"] or 0.0, "runs": r["runs"], "workouts": r["workouts"] or 0}
        for r in week_rows
    ]
    periods, _gaps = detect_periods(weeks)
    if not periods:
        return [], []

    race_activity_rows = conn.execute(f"""
        SELECT DATE(start_date) AS date, name, distance_m
        FROM activities
        WHERE {type_clause} AND {effective_run_type} = 'race'
        ORDER BY date
    """, type_params).fetchall()

    race_refs: list[RaceRef] = [
        {
            "date": r["date"],
            "name": r["name"],
            "distance_category": classify_race_distance(r["distance_m"]) or "other",
            "distance_m": r["distance_m"],
        }
        for r in race_activity_rows
        if r["distance_m"] is not None
    ]
    builds = detect_builds(weeks, race_refs, periods)
    return periods, builds


def _window_active_weeks(conn: sqlite3.Connection, race_week_monday: date, build_weeks: int) -> int:
    """
    Count Monday-aligned weeks in [race_week_monday - build_weeks, race_week_monday)
    meeting the active-week definition (periods.is_active), zero-filling calendar gaps.
    """
    window_start = race_week_monday - timedelta(weeks=build_weeks)
    type_clause, type_params = _run_type_filter()

    week_rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles,
            COUNT(*) AS runs
        FROM activities
        WHERE {type_clause}
          AND DATE(start_date) >= ? AND DATE(start_date) < ?
        GROUP BY monday
    """, type_params + [window_start.isoformat(), race_week_monday.isoformat()]).fetchall()

    by_monday = {r["monday"]: r for r in week_rows}
    active = 0
    d = window_start
    while d < race_week_monday:
        wr = by_monday.get(d.isoformat())
        week: WeekAgg = {
            "monday": d.isoformat(),
            "miles": (wr["miles"] or 0.0) if wr else 0.0,
            "runs": (wr["runs"] or 0) if wr else 0,
            "workouts": 0,
        }
        if is_active(week):
            active += 1
        d += timedelta(weeks=1)
    return active


def _window_coverage(
    conn: sqlite3.Connection,
    race_date_str: str,
    race_week_monday: date,
    build_weeks: int,
    periods: list[Period],
    builds: list[Build],
) -> dict[str, object]:
    """Assemble the window_coverage block shared by get_build_snapshot and get_race_comparison."""
    period = next((p for p in periods if p["start"] <= race_date_str <= p["end"]), None)
    detected_build = next((b for b in builds if b["race"]["date"] == race_date_str), None)
    return {
        "weeks_total": build_weeks,
        "active_weeks": _window_active_weeks(conn, race_week_monday, build_weeks),
        "period": {"start": period["start"], "end": period["end"]} if period else None,
        "detected_build": (
            {
                "start": detected_build["start"],
                "end": detected_build["end"],
                "weeks": detected_build["weeks"],
                "bounded_by": detected_build["bounded_by"],
                "thin": detected_build["thin"],
            }
            if detected_build else None
        ),
    }


@mcp.tool()
def get_build_snapshot(race_date: str | None = None, build_weeks: int = 12) -> str:
    """
    Week-by-week breakdown of a training build for any race distance.
    race_date: YYYY-MM-DD of the target race. If several races share that date, the
    longest one is used. If omitted, uses the most recent race of any distance in the DB.
    Returns: race info (including distance_category and run_type_source), weeks_to_race
    (negative = past race), week-by-week mileage with workout/long-run counts, all
    workout sessions with rep stats, and long run list.

    window_coverage reports whether the fixed build_weeks window was actually trained
    through: active_weeks vs. weeks_total (Monday-aligned), the enclosing detected
    training period (from get_training_periods), and detected_build — the race-anchored,
    data-derived preparation window (see get_training_periods) for this exact race, or
    null when the race is below the anchor floor (shorter than 10K) or no build was
    detected. When active_weeks is well below weeks_total (roughly under 60%), the fixed
    window wasn't a real build — describe the detected period/build instead.
    detected_build is the honest answer for a consistent year-round runner, for whom a
    fixed calendar window is otherwise arbitrary.

    Use this to orient at the start of any build-specific conversation.
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()
    effective_run_type = db.effective_run_type_sql()

    if race_date:
        race_date_str = race_date
        race_row = conn.execute(f"""
            SELECT name, distance_m, moving_time_s, average_speed_mps,
                   workout_type, run_type_inferred
            FROM activities
            WHERE {type_clause} AND {effective_run_type} = 'race'
              AND DATE(start_date) = ?
            ORDER BY distance_m DESC LIMIT 1
        """, type_params + [race_date]).fetchone()
    else:
        race_row = conn.execute(f"""
            SELECT name, DATE(start_date) AS race_date, distance_m, moving_time_s, average_speed_mps,
                   workout_type, run_type_inferred
            FROM activities
            WHERE {type_clause} AND {effective_run_type} = 'race'
            ORDER BY start_date DESC LIMIT 1
        """, type_params).fetchone()
        if not race_row:
            return json.dumps({"error": "No race found in the database."})
        race_date_str = race_row["race_date"]

    race_name: str | None = race_row["name"] if race_row else None
    distance_m: float | None = race_row["distance_m"] if race_row else None
    distance_category = classify_race_distance(distance_m) or "other"
    run_type_source: str | None = None
    race_result: dict[str, object] | None = None
    if race_row:
        run_type_source = (
            "inferred" if race_row["workout_type"] == 0 and race_row["run_type_inferred"] is not None
            else "strava"
        )
        avg_speed = race_row["average_speed_mps"]
        race_result = {
            "moving_time_s": race_row["moving_time_s"],
            "distance_miles": round(distance_m / 1609.34, 2) if distance_m is not None else None,
            "pace_min_per_mile": round(26.8224 / avg_speed, 2) if avg_speed and avg_speed > 0 else None,
        }

    race_dt = date.fromisoformat(race_date_str)
    race_week_monday = race_dt - timedelta(days=race_dt.weekday())
    build_start = (race_week_monday - timedelta(weeks=build_weeks)).isoformat()
    weeks_to_race = (race_dt - date.today()).days // 7

    periods, builds = _full_periods_and_builds(conn)
    window_coverage = _window_coverage(conn, race_date_str, race_week_monday, build_weeks, periods, builds)

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
        "distance_category": distance_category,
        "run_type_source": run_type_source,
        "build_start": build_start,
        "weeks_to_race": weeks_to_race,
        "weeks": [dict(w) for w in weeks],
        "workouts": [dict(w) for w in workouts],
        "long_runs": [dict(lr) for lr in long_runs],
        "window_coverage": window_coverage,
    }))


@mcp.tool()
def get_workout_session(activity_id: int) -> str:
    """
    Detailed view of a single workout: all laps in sequence, each classified by lap_type:
      warmup | work | recovery (jog between reps) | float (slow-but-still-work laps,
      e.g. MP flux slow halves) | cooldown | steady (no interval structure detected).
    Trivial laps (< 200m or < 45s) are filtered out.
    Each lap: lap_num, lap_type, intensity, distance_miles, duration_s, pace_min_mi, avg_hr, max_hr.
    intensity names what a work/float lap was run at relative to the athlete's estimated
    fitness at the time — MP | threshold | interval | repetition | aerobic | sprint |
    sub-<zone>; null for a non-work lap, a trivial lap, or a month with no reliable
    fitness estimate. dominant_intensity (session level) is the intensity holding >=60%
    of work-lap time, null when mixed or unavailable — it can stand in for workout_label
    on unlabeled sessions.
    Use this to inspect within-session structure — whether reps held even, drifted,
    or fell apart — rather than relying solely on session averages.
    activity_id comes from get_build_snapshot, compare_workouts_by_build, or get_activities.
    """
    conn = _conn()

    activity = conn.execute("""
        SELECT activity_id, name, DATE(start_date) AS date, workout_label, dominant_intensity,
               ROUND(distance_m / 1609.34, 2) AS total_miles,
               moving_time_s AS total_time_s, strava_url
        FROM activities WHERE activity_id = ?
    """, [activity_id]).fetchone()

    if not activity:
        return json.dumps({"error": f"Activity {activity_id} not found."})

    laps = conn.execute("""
        SELECT
            lap_index,
            lap_type,
            intensity,
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

    keep = ("lap_index", "distance_miles", "duration_s", "pace_min_mi", "avg_hr", "max_hr", "intensity")
    return json.dumps(_fmt_paces({
        **dict(activity),
        "laps": [
            {"lap_num": i + 1, "lap_type": r["lap_type"], **{k: r[k] for k in keep}}
            for i, r in enumerate(laps)
        ],
    }))


_RACE_SPLITS_META_KEYS = (
    "date", "name", "distance_category", "distance_miles",
    "finish_time", "finish_time_s", "pace_min_per_mile", "strava_url",
)


@mcp.tool()
def get_race_splits(activity_id: int) -> str:
    """
    Post-race split analysis: how the effort was paced across the two halves of the
    race, and where it fell apart (if it did). split_type is negative (back half
    ≥1% faster), even (within 1%), or positive (back half slower) — the standard
    three-way pacing verdict. fade_pct > 0 means the athlete slowed in the second half.

    Laps come from the athlete's watch (manual lap presses or Garmin auto-lap),
    synced by miles-sync once an activity is tagged/inferred as a race — only
    available after a sync has fetched that race's laps.

    Returns race metadata (date, name, distance_category, distance_miles,
    finish_time, finish_time_s, pace_min_per_mile, strava_url) plus:
      laps: [{lap_num, distance_miles, duration_s, pace_min_mi, avg_hr, max_hr,
              cumulative_time_s}]
      summary: {first_half_pace, second_half_pace, split_type, fade_pct,
                fastest_lap, slowest_lap}  (fastest/slowest are lap_num values)
    Trivial laps (< 200m or < 45s) are excluded before splitting; the straddling
    lap at the halfway point is apportioned by distance.
    """
    conn = _conn()

    races = race_rows(conn, activity_id=activity_id)
    if not races:
        exists = conn.execute(
            "SELECT 1 FROM activities WHERE activity_id = ?", [activity_id]
        ).fetchone()
        if exists is None:
            return json.dumps({"error": f"Activity {activity_id} not found."})
        return json.dumps({"error": f"Activity {activity_id} is not a race."})
    race_meta = {k: races[0][k] for k in _RACE_SPLITS_META_KEYS}

    laps = conn.execute("""
        SELECT
            lap_index,
            distance_m,
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

    if not laps:
        return json.dumps({"error": "No laps synced for this race — run miles-sync."})

    lap_dicts: list[dict[str, object]] = []
    cumulative_s = 0
    for i, lap in enumerate(laps):
        cumulative_s += int(lap["duration_s"])
        lap_dicts.append({
            "lap_num": i + 1,
            "distance_miles": lap["distance_miles"],
            "duration_s": lap["duration_s"],
            "pace_min_mi": lap["pace_min_mi"],
            "avg_hr": lap["avg_hr"],
            "max_hr": lap["max_hr"],
            "cumulative_time_s": cumulative_s,
        })

    total_distance_m = sum(float(lap["distance_m"]) for lap in laps)
    total_time_s = sum(float(lap["duration_s"]) for lap in laps)

    # Split at half the total distance, apportioning the straddling lap by distance.
    remaining_half_m = total_distance_m / 2
    first_half_time_s = 0.0
    first_half_distance_m = 0.0
    for lap in laps:
        if remaining_half_m <= 0:
            break
        lap_distance_m = float(lap["distance_m"])
        lap_duration_s = float(lap["duration_s"])
        if lap_distance_m <= remaining_half_m:
            first_half_time_s += lap_duration_s
            first_half_distance_m += lap_distance_m
            remaining_half_m -= lap_distance_m
        else:
            frac = remaining_half_m / lap_distance_m
            first_half_time_s += lap_duration_s * frac
            first_half_distance_m += remaining_half_m
            remaining_half_m = 0.0

    second_half_time_s = total_time_s - first_half_time_s
    second_half_distance_m = total_distance_m - first_half_distance_m

    first_half_pace = (first_half_time_s / 60) / (first_half_distance_m / 1609.34)
    second_half_pace = (second_half_time_s / 60) / (second_half_distance_m / 1609.34)
    fade_pct = (second_half_pace / first_half_pace - 1) * 100
    if second_half_pace <= first_half_pace * 0.99:
        split_type = "negative"
    elif second_half_pace < first_half_pace * 1.01:
        split_type = "even"
    else:
        split_type = "positive"

    paces = [float(lap["pace_min_mi"]) for lap in laps]
    fastest_idx = min(range(len(paces)), key=lambda i: paces[i])
    slowest_idx = max(range(len(paces)), key=lambda i: paces[i])

    return json.dumps(_fmt_paces({
        **race_meta,
        "laps": lap_dicts,
        "summary": {
            "first_half_pace": round(first_half_pace, 2),
            "second_half_pace": round(second_half_pace, 2),
            "split_type": split_type,
            "fade_pct": round(fade_pct, 1),
            "fastest_lap": lap_dicts[fastest_idx]["lap_num"],
            "slowest_lap": lap_dicts[slowest_idx]["lap_num"],
        },
    }))


@mcp.tool()
def get_easy_hr_trend(months: int = 36) -> str:
    """
    Monthly average HR and pace for easy runs — the primary long-term aerobic fitness signal.
    A declining HR trend at stable or faster paces indicates improving aerobic efficiency
    accumulated across builds, not attributable to any single cycle.
    Returns months with avg_hr, avg_pace_min_mi, run_count. When the athlete has a configured
    max HR, each month also carries avg_pct_max (avg_hr as % of max HR) — conventionally,
    easy running sits around 70-80% of max. Omitted entirely when no max HR is configured.
    Filtered to easy-tagged runs only.
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

    results = [dict(r) for r in rows]
    athlete = db.get_athlete(conn)
    max_hr = athlete["max_hr"] if athlete else None
    if max_hr:
        for r in results:
            if r["avg_hr"] is not None:
                r["avg_pct_max"] = round(100 * r["avg_hr"] / max_hr, 1)

    return json.dumps(_fmt_paces(results))


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
        SELECT activity_id, average_speed_mps, average_heartrate
        FROM laps
        WHERE activity_id IN ({placeholders}) AND lap_type = 'work'
        ORDER BY activity_id, lap_index
    """, id_list).fetchall()

    # Group rep laps by activity
    laps_by_id: dict[int, list[sqlite3.Row]] = {aid: [] for aid in id_list}
    for lap in lap_rows:
        laps_by_id[int(lap["activity_id"])].append(lap)

    # Compute per-session rep stats over work laps
    session_stats: dict[int, dict[str, object]] = {}
    for activity_id, rep_laps in laps_by_id.items():
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
def get_fitness_estimate(as_of: str | None = None) -> str:
    """
    Predicted race paces (5K/10K/half/marathon) and derived training zones as of a
    date (YYYY-MM-DD, default today), from the best signal in the trailing 180 days:
    races (confidence high/medium), classified workout laps (medium-low), or the
    fastest sustained training run scaled to a race-pace envelope (low).

    When quoting this, ALWAYS state `confidence` and cite `sources`. `low` is a
    floor derived from training paces, not a fitness fact — phrase it as "at
    least". Predictions assume race-specific training: they get less reliable the
    further a target distance is from what the athlete recently raced or trained
    for. A `note` appears when the newest race is stale and a fresher lower-tier
    signal predicts faster — relay it rather than silently picking a number.

    Zones: easy_range (marathon pace +1:00 to +1:45/mi), marathon, threshold
    (15K-equivalent), interval (5K), repetition (mile-equivalent). All paces are
    M:SS per mile. Returns {"error": ...} when there is no signal in the window.
    """
    conn = _conn()
    try:
        as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        return json.dumps({"error": f"Invalid as_of date: {as_of!r} (expected YYYY-MM-DD)."})

    est = estimate_fitness(conn, as_of_date)
    if est is None:
        return json.dumps({
            "error": f"No fitness signal in the trailing {WINDOW_DAYS} days before {as_of_date.isoformat()}."
        })

    out: dict[str, object] = {
        "as_of": est["as_of"],
        "confidence": est["confidence"],
        "predicted": {k: fmt_pace(v) for k, v in est["predicted"].items()},
        "zones": {
            k: (v if isinstance(v, str) else fmt_pace(v))
            for k, v in est["zones"].items()
        },
        "sources": est["sources"],
    }
    if "note" in est:
        out["note"] = est["note"]
    return json.dumps(out)


@mcp.tool()
def get_fitness_trend(months: int = 36) -> str:
    """
    Estimated race-pace trend over time: one checkpoint per calendar month with
    any fitness signal, read from the fitness_checkpoints table (derived, rebuilt
    each sync). Each row: month, confidence, source_tier (1 = races, 2 = workout
    laps, 3 = envelope floor), and predicted 5K/10K/half/marathon paces (M:SS/mi).
    Use it for "am I fitter than last year?" — expect paces to drift slower after
    breaks and sharpen toward races. Low-confidence months are floors, not facts.
    For a full estimate with zones and evidence at an arbitrary date, use
    get_fitness_estimate.
    """
    conn = _conn()
    cutoff = (date.today() - timedelta(days=months * 30)).strftime("%Y-%m")
    rows = conn.execute("""
        SELECT month, confidence, source_tier, pace_5k, pace_10k, pace_half, pace_marathon
        FROM fitness_checkpoints
        WHERE month >= ?
        ORDER BY month
    """, [cutoff]).fetchall()

    return json.dumps([
        {
            "month": r["month"],
            "confidence": r["confidence"],
            "source_tier": r["source_tier"],
            **{
                key: (fmt_pace(float(r[col])) if r[col] is not None else None)
                for key, col in (
                    ("pace_5k", "pace_5k"), ("pace_10k", "pace_10k"),
                    ("pace_half", "pace_half"), ("pace_marathon", "pace_marathon"),
                )
            },
        }
        for r in rows
    ])


# --- Plan tools -------------------------------------------------------------
#
# Plans are athlete-authored ground truth (plans/plan_versions/plan_weeks/
# plan_days/plan_log) — the first write surface in miles. Every write is a
# thin wrapper over miles/plan.py, which owns all validation; these tools only
# shape the response and catch PlanValidationError into a friendly {"error":
# ...} naming the offending week/day/field. Never let a raw exception escape —
# a broad except is the last-resort net for genuinely malformed input.

# Sanity-warning thresholds (advisory only, never block a write). Tunable.
WARN_WEEK1_MULT = 1.3   # week-1 target vs. recent 4-week average mileage
WARN_RAMP_PCT = 0.10    # week-over-week mileage ramp considered aggressive
WARN_RAMP_MIN_WEEKS = 3  # consecutive ramp weeks needed before warning


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _recent_avg_weekly_miles(conn: sqlite3.Connection, as_of: date, num_weeks: int = 4) -> float | None:
    """Average mileage over the num_weeks Monday-aligned weeks completed
    strictly before as_of's week, zero-filled. None if the athlete has no
    activities at all (vs. a real 0.0 average during a genuine break)."""
    if conn.execute("SELECT 1 FROM activities LIMIT 1").fetchone() is None:
        return None
    this_monday = _monday_of(as_of)
    window_start = this_monday - timedelta(weeks=num_weeks)
    type_clause, type_params = _run_type_filter()
    rows = conn.execute(f"""
        SELECT
            DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
            ROUND(SUM(distance_m) / 1609.34, 2) AS miles
        FROM activities
        WHERE {type_clause} AND DATE(start_date) >= ? AND DATE(start_date) < ?
        GROUP BY monday
    """, type_params + [window_start.isoformat(), this_monday.isoformat()]).fetchall()
    by_monday = {r["monday"]: (r["miles"] or 0.0) for r in rows}
    total = sum(by_monday.get((window_start + timedelta(weeks=i)).isoformat(), 0.0) for i in range(num_weeks))
    return round(total / num_weeks, 1)


def _all_time_peak_week_miles(conn: sqlite3.Connection) -> float | None:
    """Max Monday-aligned weekly mileage across the athlete's full history, or
    None if there are no activities."""
    type_clause, type_params = _run_type_filter()
    row = conn.execute(f"""
        SELECT MAX(weekly_miles) AS peak FROM (
            SELECT
                DATE(start_date, '-' || ((CAST(strftime('%w', start_date) AS INTEGER) + 6) % 7) || ' days') AS monday,
                SUM(distance_m) / 1609.34 AS weekly_miles
            FROM activities
            WHERE {type_clause}
            GROUP BY monday
        )
    """, type_params).fetchone()
    return round(row["peak"], 1) if row is not None and row["peak"] is not None else None


def _sanity_warnings(
    conn: sqlite3.Connection, weeks: Sequence[WeekInput | db.PlanWeekRow], as_of: date
) -> list[str]:
    """Advisory (never rejecting) warnings on a draft/revised plan's weeks,
    computed against the activities table: week-1 target vs. recent volume,
    peak week vs. all-time peak, and sustained aggressive ramp. Thresholds are
    the WARN_* module constants above. Tolerant of a partial draft (any
    subset of weeks, in any order) and of deliberately unspecified weeks
    (target_miles NULL) — those weeks are simply excluded from the mileage
    checks that need a number, same as if they weren't authored yet.
    """
    warnings: list[str] = []
    if not weeks:
        return warnings
    sorted_weeks = sorted(weeks, key=lambda w: w["week_start"])
    # (week_start, target_miles) pairs, restricted to weeks with a mileage
    # target — a plain (str, float) tuple sidesteps the NotRequired-key
    # access checks entirely once extracted.
    mileage: list[tuple[str, float]] = [
        (w["week_start"], m) for w in sorted_weeks if (m := w.get("target_miles")) is not None
    ]

    if mileage:
        week1_start, week1_miles = mileage[0]
        recent_avg = _recent_avg_weekly_miles(conn, as_of)
        if recent_avg is not None and recent_avg > 0 and week1_miles > WARN_WEEK1_MULT * recent_avg:
            warnings.append(
                f"week 1 ({week1_start}) targets {week1_miles} mi, "
                f"{week1_miles / recent_avg:.1f}x the recent 4-week average of {recent_avg} mi"
            )

        peak_start, peak_miles = max(mileage, key=lambda p: p[1])
        all_time_peak = _all_time_peak_week_miles(conn)
        if all_time_peak is not None and peak_miles > all_time_peak:
            warnings.append(
                f"peak week ({peak_start}, {peak_miles} mi) exceeds the "
                f"athlete's all-time peak week ({all_time_peak} mi)"
            )

    ramp_flags = [False] + [
        prev_m is not None and cur_m is not None and prev_m > 0
        and (cur_m - prev_m) / prev_m > WARN_RAMP_PCT
        for prev_m, cur_m in zip(
            (w.get("target_miles") for w in sorted_weeks),
            (w.get("target_miles") for w in sorted_weeks[1:]),
        )
    ]
    i = 1
    while i < len(ramp_flags):
        if not ramp_flags[i]:
            i += 1
            continue
        j = i
        while j < len(ramp_flags) and ramp_flags[j]:
            j += 1
        if j - i >= WARN_RAMP_MIN_WEEKS:
            warnings.append(
                f"{j - i} consecutive weeks ramp mileage >{WARN_RAMP_PCT * 100:.0f}%/wk "
                f"({sorted_weeks[i - 1]['week_start']} to {sorted_weeks[j - 1]['week_start']})"
            )
        i = j

    return warnings


def _day_with_target(d: db.PlanDayRow) -> dict[str, object]:
    """A plan_days row with target_json parsed into a nested `target` dict
    (so _fmt_paces can format its pace_lo/pace_hi) instead of a raw JSON string."""
    out: dict[str, object] = dict(d)
    target_json = out.pop("target_json", None)
    out["target"] = json.loads(cast(str, target_json)) if target_json else None
    return out


def _plan_start_monday(conn: sqlite3.Connection, plan_id: int) -> date | None:
    """The plan's first week (from version 1, which always governs from the
    plan's start regardless of its own created_at), or None if v1 has no weeks
    (shouldn't happen for an active plan)."""
    row = conn.execute("""
        SELECT MIN(pw.week_start) AS d
        FROM plan_weeks pw JOIN plan_versions pv ON pv.version_id = pw.version_id
        WHERE pv.plan_id = ? AND pv.version_n = 1
    """, [plan_id]).fetchone()
    return date.fromisoformat(row["d"]) if row is not None and row["d"] is not None else None


def _days_stale(last_sync_at: str | None) -> int | None:
    """Whole days between last_sync_at (meta.last_sync_at, a UTC ISO
    timestamp stamped by miles-sync) and today, or None when the DB has
    never been synced. A plan tool surfacing this alongside last_sync_at
    lets a planner catch "0 miles this week" reading as a stale sync rather
    than a slow start before it narrates the number."""
    if last_sync_at is None:
        return None
    synced_date = datetime.fromisoformat(last_sync_at).date()
    return (date.today() - synced_date).days


def _resolve_draft(conn: sqlite3.Connection) -> tuple[int, int] | dict[str, str]:
    """Locates the one in-progress draft (a plan_versions row with
    committed_at IS NULL) and returns (plan_id, version_id). Every
    draft-editing tool takes no plan_id — normal usage has at most one draft
    open at a time — so this returns a named {"error": ...} dict instead of
    raising when there is none, or (a multi-plan-drafting edge case: a new
    plan drafted while a revision draft is also open on another plan) more
    than one, since neither tool has any way to disambiguate."""
    rows = conn.execute(
        "SELECT pv.plan_id AS plan_id, pv.version_id AS version_id, p.title AS title "
        "FROM plan_versions pv JOIN plans p ON p.plan_id = pv.plan_id "
        "WHERE pv.committed_at IS NULL"
    ).fetchall()
    if not rows:
        return {"error": "no draft in progress; call start_plan_draft or start_revision_draft first"}
    if len(rows) > 1:
        plans = ", ".join(f"plan_id={int(r['plan_id'])} ({r['title']!r})" for r in rows)
        return {"error": f"multiple drafts in progress ({plans}); specify by discarding or committing one first"}
    return int(rows[0]["plan_id"]), int(rows[0]["version_id"])


def _draft_state(conn: sqlite3.Connection, plan_id: int) -> dict[str, object]:
    """Full draft-read payload shared by every draft tool's response: current
    weeks/days, the gap report (plan._draft_gap_report, via get_draft — what
    commit_plan's global validation would reject right now), sanity warnings
    (see _sanity_warnings) computed over whatever weeks exist so far —
    tolerant of a partial draft, since _sanity_warnings already no-ops on an
    empty or partially-unspecified week list — and sync freshness."""
    bundle = _plan_get_draft(conn, plan_id)
    last_sync_at = db.get_last_sync_at(conn)
    return {
        "plan": bundle["plan"],
        "version": bundle["version"],
        "weeks": bundle["weeks"],
        "days": [_day_with_target(d) for d in bundle["days"]],
        "gaps": bundle["gaps"],
        "warnings": _sanity_warnings(conn, bundle["weeks"], date.today()),
        "last_sync_at": last_sync_at,
        "days_stale": _days_stale(last_sync_at),
    }


@mcp.tool()
def start_plan_draft(
    title: str,
    race_date: str,
    distance_bucket: str,
    goal_time_s: int | None = None,
) -> str:
    """
    Starts a brand-new plan's mutable draft: a plans row (status='draft') plus
    an empty first (draft) version. The only write path for a plan from
    scratch — follow with set_draft_weeks / set_draft_days in batches as the
    athlete approves each block, get_draft to narrate what's missing, and
    commit_plan once they've seen the whole thing and said go.

    Unlike commit_plan, this never rejects for an already-active plan — a
    draft may coexist with one (drafting next season during a taper is
    normal); the "only one active plan" rule is enforced at commit time.

    Returns the new draft's state (see get_draft's return shape) on success,
    or {"error": "..."} naming the offending field (empty title, invalid
    race_date, negative goal_time_s) — never a traceback.
    """
    conn = _conn()
    try:
        try:
            plan_id, _version_id = _plan_start_plan_draft(
                conn, title=title, race_date=race_date, distance_bucket=distance_bucket,
                goal_time_s=goal_time_s,
            )
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(_fmt_paces(_draft_state(conn, plan_id)))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def start_revision_draft() -> str:
    """
    Starts a revision draft for the active plan: copies its current (latest
    committed) version's weeks/days verbatim into a new draft version, which
    the athlete then edits incrementally via set_draft_weeks/set_draft_days —
    only the weeks that actually change need a call; everything else is
    already there from the copy. Rejects if the active plan already has a
    draft in progress (one draft per plan — commit or discard it first).

    Check freshness (the response's last_sync_at/days_stale) before revising
    off the current numbers — a stale sync means the athlete's recent
    training isn't reflected yet, not that it didn't happen.

    Returns the new draft's state (see get_draft's return shape) on success,
    or {"error": "..."}: no active plan, or a draft already in progress.
    """
    conn = _conn()
    try:
        p = get_active_plan(conn)
        if p is None:
            return json.dumps({"error": "no active plan to revise"})
        try:
            _plan_start_revision_draft(conn, p["plan_id"])
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(_fmt_paces(_draft_state(conn, p["plan_id"])))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def get_draft() -> str:
    """
    Current state of the one in-progress draft (started via start_plan_draft
    or start_revision_draft): its weeks/days as authored so far — zone-
    anchored day targets stay unresolved until commit_plan freezes them —
    plus:

    gaps: plain-English messages naming what commit_plan's global validation
      would reject right now ("weeks 6-20 unauthored", "week of 2026-08-03
      has days but no week row", "weeks don't reach race week..."). Narrate
      these honestly rather than assuming a batch landed clean.
    warnings: advisory sanity checks (week-1 target vs. recent volume, peak
      week vs. all-time peak, sustained aggressive ramp — see commit_plan),
      computed over whatever weeks exist so far — never blocking, and never
      raised for a subset of weeks that simply hasn't been authored yet.
    last_sync_at / days_stale: whole days since the last miles-sync (null if
      never synced). Stale means the pipeline hasn't seen recent training
      yet — missing data, not missing training — check this before
      narrating "how's this week going" mid-draft.

    Returns {"error": "no draft in progress"} if none exists (or a named
    ambiguity error in the rare case more than one plan has an open draft).
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, _version_id = resolved
        return json.dumps(_fmt_paces(_draft_state(conn, plan_id)))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def set_draft_weeks(weeks: list[WeekInput]) -> str:
    """
    Upserts any subset of the in-progress draft's weeks, by week_start — call
    it in batches as the athlete approves each block ("here's the first 5
    weeks, look good?"); a partial batch that leaves gaps mid-plan is
    expected, not an error. Only local field validation runs here (Monday-
    aligned, non-negative targets, known phase, target_miles_hi >=
    target_miles); contiguity and reaching the race week are commit_plan's
    job, since a partial draft legitimately doesn't satisfy those yet.

    weeks: list of {week_start (Monday, YYYY-MM-DD), target_miles,
      target_workouts, phase (base|sharpen|peak|taper|race),
      target_miles_hi? (range upper bound; both-NULL-equivalent — omit
      target_miles_hi for a point week), target_long_run_miles?,
      target_long_run_minutes?, target_strength_days?, note?}.

    Finishes by re-running the full derive pass so plan_adherence reflects
    the edit immediately (see derive_all) — matters most for a revision that
    touches an already-elapsed week.

    Returns the full draft state (see get_draft) plus `written: {"weeks":
    [...week_starts...]}` on success, or {"error": "..."} naming the
    offending week/field — never a traceback.
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, version_id = resolved
        try:
            upsert_draft_weeks(conn, version_id, weeks)
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        derive_all(conn)
        result = _draft_state(conn, plan_id)
        result["written"] = {"weeks": sorted({w["week_start"] for w in weeks})}
        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def set_draft_days(days: list[DayInput]) -> str:
    """
    Upserts any subset of the in-progress draft's days, by (date, seq) — call
    alongside set_draft_weeks. A day may be authored before its week row
    exists; get_draft's gap report will say so, this call won't reject it.

    days: list of {date, slot (easy|workout|long|rest|race|strength), seq?
      (default 1; 2+ = doubles), title?, target_miles?, target_minutes?,
      terrain? (road|trail; default road — composes with any run slot rather
      than being one itself: a trail long run is still a long run, and its
      pace targets are display-guidance only, never scored), note? (athlete-
      facing guidance, e.g. "8-10 x 3min LT, go to 10 if feeling great"),
      target?}. target is a DayTarget: {reps?, reps_lo?, reps_hi? (a rep
      range; reps stays the point form), rep_duration_s?, rep_distance_m?,
      pace_lo?, pace_hi?, zone_name?, hr_lo?, hr_hi?}. A zone_name with no
      explicit pace_lo/pace_hi is stored unresolved and freezes against a
      live fitness estimate at commit_plan time, not now.

    Finishes by re-running the full derive pass (see derive_all).

    Returns the full draft state (see get_draft) plus `written: {"days":
    [[date, seq], ...]}` on success, or {"error": "..."} naming the
    offending day/field — never a traceback.
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, version_id = resolved
        try:
            upsert_draft_days(conn, version_id, days)
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        derive_all(conn)
        result = _draft_state(conn, plan_id)
        result["written"] = {"days": [[d["date"], d.get("seq", 1)] for d in days]}
        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def delete_draft_weeks(week_starts: list[str]) -> str:
    """
    Deletes the given week_starts from the in-progress draft, and any days
    that fall inside them (no orphan day rows left behind). No-ops on
    week_starts not present in the draft.

    Finishes by re-running the full derive pass (see derive_all).

    Returns the full draft state (see get_draft) plus `deleted_weeks: N` on
    success, or {"error": "..."} — no draft in progress.
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, version_id = resolved
        try:
            deleted = _plan_delete_draft_weeks(conn, version_id, week_starts)
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        derive_all(conn)
        result = _draft_state(conn, plan_id)
        result["deleted_weeks"] = deleted
        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def delete_draft_days(dates: list[str]) -> str:
    """
    Deletes days from the in-progress draft by date (YYYY-MM-DD) — every seq
    for that date is removed, so a double counts as one delete. No-ops on
    dates not present in the draft.

    Finishes by re-running the full derive pass (see derive_all).

    Returns the full draft state (see get_draft) plus `deleted_days: N` on
    success, or {"error": "..."} — no draft in progress.
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, version_id = resolved
        try:
            deleted = _plan_delete_draft_days(conn, version_id, cast("list[str | tuple[str, int]]", dates))
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        derive_all(conn)
        result = _draft_state(conn, plan_id)
        result["deleted_days"] = deleted
        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def commit_plan(note: str) -> str:
    """
    Commits the in-progress draft: runs full global validation (contiguous
    Mondays, ends at the race week, every day falls in a committed week),
    re-freezes every zone-anchored day target as of today (commit IS
    "authoring" for the freeze rule, even for weeks authored earlier), stamps
    committed_at, and flips the plan to status='active'. Requires a
    non-empty `note` — the athlete-approval record; there is no commit
    without one, and the plan is never live until the athlete has seen it
    and said go.

    For a revision (the plan already has a prior committed version): any
    week on or before the start of this week — already governing or already
    in progress — is silently overwritten with whatever version actually
    governed it at the time, regardless of what the draft proposed for that
    week or its days. This makes rewriting history to look adherent
    structurally impossible. `past_weeks_preserved` in the response lists
    which week_starts were protected this way; it's always empty for a
    plan's first-ever commit (a backdated week 1 is legitimate authored
    history — mid-block import — not something to protect from itself).

    Rejects committing a brand-new plan while a *different* plan is already
    active (a draft may coexist with one, but only one plan can hold the
    active slot); revising the currently-active plan itself is always
    allowed.

    Finishes by re-running the full derive pass, so plan_adherence rows
    exist for any already-elapsed weeks immediately after this call (see
    derive_all) — the point of a mid-block import isn't a plan that scores
    nothing until the next sync.

    Returns {"plan": ..., "version_n": ..., "week_count": ..., "weeks": [...],
    "days": [...], "warnings": [...], "past_weeks_preserved": [...]} on
    success, or {"error": "..."} naming the offending week/day/field/note —
    never a traceback. warnings are the same advisory sanity checks (see
    _sanity_warnings): week-1 target > 1.3x recent 4-week average; peak week
    exceeds the athlete's all-time peak; 3+ consecutive weeks ramping
    mileage >10%/week.
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, version_id = resolved

        # Mirrors _snapshot_past_weeks' own bounds (plan.py) purely for
        # reporting — commit_plan there doesn't return which weeks it
        # protected, so recompute the same walk read-only, before the write,
        # to know what to tell the athlete.
        has_prior_committed = conn.execute(
            "SELECT 1 FROM plan_versions WHERE plan_id = ? AND committed_at IS NOT NULL LIMIT 1",
            [plan_id],
        ).fetchone() is not None
        past_weeks_preserved: list[str] = []
        if has_prior_committed:
            this_monday = _monday_of(date.today())
            plan_start = _plan_start_monday(conn, plan_id)
            if plan_start is not None:
                m = plan_start
                while m <= this_monday:
                    past_weeks_preserved.append(m.isoformat())
                    m += timedelta(weeks=1)

        try:
            _plan_commit_plan(conn, plan_id, note=note)
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})

        derive_all(conn)

        bundle = get_version(conn, version_id)
        assert bundle is not None
        result = {
            "plan": get_active_plan(conn),
            "version_n": bundle["version"]["version_n"],
            "week_count": len(bundle["weeks"]),
            "weeks": bundle["weeks"],
            "days": [_day_with_target(d) for d in bundle["days"]],
            "warnings": _sanity_warnings(conn, bundle["weeks"], date.today()),
            "past_weeks_preserved": past_weeks_preserved,
        }
        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def discard_draft() -> str:
    """
    Deletes the in-progress draft's weeks/days and its version row. If the
    plan itself was never committed (a from-scratch draft, not a revision),
    the plan row is deleted too — a never-committed plan simply disappears,
    same as it never existed.

    Finishes by re-running the full derive pass — a no-op here since
    discarding never touches a committed version, kept only for consistency
    with the other write tools.

    Returns {"discarded_plan_id": ...} on success, or {"error": "..."} — no
    draft in progress.
    """
    conn = _conn()
    try:
        resolved = _resolve_draft(conn)
        if isinstance(resolved, dict):
            return json.dumps(resolved)
        plan_id, _version_id = resolved
        try:
            _plan_discard_draft(conn, plan_id)
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        derive_all(conn)
        return json.dumps({"discarded_plan_id": plan_id})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def get_training_plan(version_n: int | None = None, compare_to: int | None = None) -> str:
    """
    Reads the active plan's committed history: a specific version_n, or the
    latest committed version by default (a revision's in-progress draft, if
    any, never surfaces here — see get_draft for that). Always includes
    `versions` (version_n, created_at, note, author for every committed
    version) and `governing_version_n` — the version_n that currently
    governs this week per the contemporaneous-version rule (may lag behind
    the latest version_n right after a mid-week revision; it takes effect
    next Monday).

    compare_to: optional version_n to diff against the returned version —
    added/removed/changed weeks and days, via plan.diff_versions (computed on
    the fly, never stored). Surfaced as result["diff"] when given.

    last_sync_at / days_stale (whole days since the last miles-sync; null
    when never synced) travel with every read. Stale actuals are missing
    data, not missing training — a sparse-looking in-progress week reflects
    what the pipeline has seen, not what the athlete did; check days_stale
    before reading anything into a quiet week.

    this_week: the in-progress week's targets (from the governing version)
    and actuals so far, cut off at actuals_as_of — the last-synced date, not
    today. Never characterize this week's volume without stating
    actuals_as_of; null when today falls outside the plan window.

    Returns {"error": "no active plan"} if there is none, or {"error": ...}
    naming the missing version_n / compare_to.
    """
    conn = _conn()
    try:
        p = get_active_plan(conn)
        if p is None:
            return json.dumps({"error": "no active plan"})
        plan_id = p["plan_id"]

        version_rows = conn.execute(
            "SELECT version_id, version_n, created_at, note, author FROM plan_versions "
            "WHERE plan_id = ? AND committed_at IS NOT NULL ORDER BY version_n", [plan_id],
        ).fetchall()
        if not version_rows:
            return json.dumps({"error": f"plan {plan_id} has no committed versions"})
        version_index = {int(r["version_n"]): int(r["version_id"]) for r in version_rows}

        target_n = version_n if version_n is not None else max(version_index)
        if target_n not in version_index:
            return json.dumps({"error": f"version_n {target_n} does not exist for plan {plan_id}"})
        bundle = get_version(conn, version_index[target_n])
        assert bundle is not None

        governing = current_version_for_week(conn, plan_id, _monday_of(date.today()))
        last_sync_at = db.get_last_sync_at(conn)

        this_week: dict[str, object] | None = None
        monday = _monday_of(date.today())
        week_row = next(
            (w for w in governing["weeks"] if w["week_start"] == monday.isoformat()),
            None,
        ) if governing else None
        if week_row is not None:
            cutoff = date.today()
            if last_sync_at is not None:
                cutoff = min(cutoff, date.fromisoformat(last_sync_at[:10]))
            if cutoff >= monday:
                tc, tp = _run_type_filter()
                effective = db.effective_run_type_sql()
                actual = conn.execute(f"""
                    SELECT ROUND(COALESCE(SUM(distance_m), 0) / 1609.34, 2) AS miles,
                           SUM(CASE WHEN {effective} = 'workout' THEN 1 ELSE 0 END) AS workouts
                    FROM activities
                    WHERE {tc} AND DATE(start_date) >= ? AND DATE(start_date) <= ?
                """, tp + [monday.isoformat(), cutoff.isoformat()]).fetchone()
                this_week = {
                    "week": week_row,
                    "actual_miles_so_far": actual["miles"],
                    "actual_workouts_so_far": actual["workouts"] or 0,
                    "actuals_as_of": cutoff.isoformat(),
                    "week_fully_synced": cutoff >= monday + timedelta(days=6),
                }
            else:
                this_week = {
                    "week": week_row,
                    "actual_miles_so_far": None,
                    "actual_workouts_so_far": None,
                    "actuals_as_of": None,
                    "week_fully_synced": False,
                }

        result: dict[str, object] = {
            "plan": p,
            "version": bundle["version"],
            "weeks": bundle["weeks"],
            "days": [_day_with_target(d) for d in bundle["days"]],
            "versions": [
                {
                    "version_n": int(r["version_n"]), "created_at": r["created_at"],
                    "note": r["note"], "author": r["author"],
                }
                for r in version_rows
            ],
            "governing_version_n": governing["version"]["version_n"] if governing else None,
            "this_week": this_week,
            "last_sync_at": last_sync_at,
            "days_stale": _days_stale(last_sync_at),
        }

        if compare_to is not None:
            if compare_to not in version_index:
                return json.dumps({"error": f"compare_to version_n {compare_to} does not exist for plan {plan_id}"})
            result["diff"] = diff_versions(conn, version_index[compare_to], version_index[target_n])

        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def log_plan_adjustment(
    log_date: str,
    action: str,
    reason: str | None = None,
    plan_id: int | None = None,
) -> str:
    """
    Records day-level reality ("skipped Tue, slept badly", "moved LT to Thu")
    without touching the plan or bumping a version — plan_log is annotation
    only; week-scoped adherence scoring already absorbs in-week moves.

    log_date: YYYY-MM-DD. action: skipped | moved | modified | note.
    plan_id defaults to the active plan.

    Returns {"log_id": ...} on success, or {"error": ...} naming the offending
    field — never a traceback.
    """
    conn = _conn()
    try:
        if plan_id is None:
            p = get_active_plan(conn)
            if p is None:
                return json.dumps({"error": "no active plan"})
            plan_id = p["plan_id"]
        try:
            log_id = add_log_entry(
                conn, plan_id, log_date=log_date,
                action=cast(Literal["skipped", "moved", "modified", "note"], action),
                reason=reason,
            )
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})
        return json.dumps({"log_id": log_id})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def abandon_plan(reason: str, plan_id: int | None = None) -> str:
    """
    Flips a plan's status to 'abandoned' and records the reason in plan_log
    (action='note'). plan_id defaults to the active plan. Frees up the active
    slot so a draft (start_plan_draft + commit_plan) can take it.

    reason is required and must be non-empty.

    Returns {"plan": {...}} on success, or {"error": ...} — no active plan,
    plan not found, plan already not active, or an empty reason.
    """
    conn = _conn()
    try:
        if not reason or not reason.strip():
            return json.dumps({"error": "abandon_plan requires a non-empty reason"})

        if plan_id is None:
            p = get_active_plan(conn)
            if p is None:
                return json.dumps({"error": "no active plan"})
            plan_id = p["plan_id"]
        else:
            row = conn.execute("SELECT plan_id, status FROM plans WHERE plan_id = ?", [plan_id]).fetchone()
            if row is None:
                return json.dumps({"error": f"plan {plan_id} does not exist"})
            if row["status"] != "active":
                return json.dumps({"error": f"plan {plan_id} is not active (status={row['status']!r})"})

        conn.execute("UPDATE plans SET status = 'abandoned' WHERE plan_id = ?", [plan_id])
        conn.commit()
        try:
            add_log_entry(conn, plan_id, log_date=date.today().isoformat(), action="note", reason=f"plan abandoned: {reason}")
        except PlanValidationError as e:
            return json.dumps({"error": str(e)})

        updated = conn.execute(
            "SELECT plan_id, title, race_date, distance_bucket, goal_time_s, status, created_at "
            "FROM plans WHERE plan_id = ?", [plan_id],
        ).fetchone()
        return json.dumps({"plan": dict(updated)})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def get_plan_adherence() -> str:
    """
    Weekly adherence for the active plan's completed weeks (week's Sunday
    before today) — the judgment layer over get_training_plan's plain
    numbers. Each week is scored against the plan version that governed it
    at the time (version_n_used), never today's plan even if it's been
    revised since — rewriting history to look adherent is structurally
    impossible.

    Each week: mileage_ratio (actual vs target; measured against the nearer
    bound for a range week, so 1.0 means "inside the band"; null when the
    week's mileage was deliberately left unspecified — those weeks are judged
    on workout count alone), actual_miles, actual_workouts,
    actual_strength_days (counted from synced strength activities; context
    only — it never moves a band or raises a flag), long_run_done
    (true/false/null — null means that week had no long-run target, not that
    it was missed; a minutes-based long-run target counts the same as a
    miles-based one), workout_pace_delta_s (seconds/mile outside the frozen
    workout pace range once +/-10s/mi slack is applied; positive = slower,
    0 = within range, null = no comparable workout data that week; trail
    workouts never contribute — grade makes road pace bands meaningless),
    and band (on | close | off — the week's overall read; a close-mileage
    week that also hit its workout count is a normal week of marathon
    training, not a miss).

    Weeks the sync hasn't fully covered are absent entirely (not scored
    "off") — check days_stale (get_training_plan) before reading a short
    list as a short history.

    `flags`: pattern-level callouts ONLY — never raised for a single day or
    a single week. A flag means 2+ *consecutive* completed weeks shared a
    real shortfall: mileage off-low, mileage off-high (sustained overshoot
    is as flag-worthy as undershoot — it's an injury-risk signal, not
    virtue), workout count short, or a missed long run. Relay flags in that
    pattern language ("workouts 1 of 2 in each of the last two weeks"), not
    as a verdict on any single week. `flags` here is empty unless a pattern
    is active as of the most recent completed week — treat silence as the
    normal, expected state, not as an incomplete read.

    `headline`: "N of M weeks on plan" — counts on+close bands together as
    "on plan" (see band note above). Lead with this number over flags.

    Returns {"error": "no active plan"} or {"error": "no completed weeks
    yet"} (a brand-new plan whose first week hasn't finished) as applicable.
    """
    conn = _conn()
    try:
        p = get_active_plan(conn)
        if p is None:
            return json.dumps({"error": "no active plan"})

        rows = conn.execute("""
            SELECT week_start, version_n_used, actual_miles, actual_workouts,
                   actual_strength_days, long_run_done, mileage_ratio,
                   workout_pace_delta_s, band, flags_json
            FROM plan_adherence WHERE plan_id = ? ORDER BY week_start
        """, [p["plan_id"]]).fetchall()
        if not rows:
            return json.dumps({"error": "no completed weeks yet"})

        weeks = [
            {
                "week_start": r["week_start"],
                "version_n_used": r["version_n_used"],
                "actual_miles": r["actual_miles"],
                "actual_workouts": r["actual_workouts"],
                "actual_strength_days": r["actual_strength_days"],
                "long_run_done": bool(r["long_run_done"]) if r["long_run_done"] is not None else None,
                "mileage_ratio": r["mileage_ratio"],
                "workout_pace_delta_s": r["workout_pace_delta_s"],
                "band": r["band"],
            }
            for r in rows
        ]
        on_or_close = sum(1 for r in rows if r["band"] in ("on", "close"))
        last_flags = json.loads(rows[-1]["flags_json"]) if rows[-1]["flags_json"] else []

        result = {
            "weeks": weeks,
            "flags": last_flags,
            "headline": f"{on_or_close} of {len(rows)} weeks on plan",
        }
        return json.dumps(_fmt_paces(result))
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def run_sql(query: str) -> str:
    """
    Run a read-only SQL SELECT against the database.
    Use this for ad-hoc questions the other tools don't cover.

    Table: activities
      activity_id, name, sport_type, start_date, workout_type, run_type, run_type_inferred,
      workout_label, distance_m, moving_time_s, elapsed_time_s, total_elevation_gain_m,
      average_speed_mps, max_speed_mps, average_heartrate, max_heartrate,
      average_cadence, gear_id, strava_url, synced_at, start_lat, start_lng,
      race_effort, effort_ratio, dominant_intensity
      (run_type_inferred is inferred for untagged rows; COALESCE with run_type via
      workout_type=0 for the effective type — see EFFECTIVE_RUN_TYPE_SQL in db.py.
      race_effort/effort_ratio are derived, rebuilt each sync — raced/hard/casual
      judged against the fitness checkpoint for the race's month plus HR; null
      when no checkpoint prediction was available for that category.
      dominant_intensity is derived, rebuilt each sync — the work-lap intensity
      (see laps.intensity) holding >=60% of a workout's work-lap time, else NULL;
      stands in for workout_label on unlabeled sessions, e.g. "all my threshold
      sessions regardless of naming" is
      WHERE dominant_intensity = 'threshold')

    Table: laps  (one row per lap; only workout activities are synced)
      lap_id, activity_id, lap_index, distance_m, moving_time_s, average_speed_mps,
      average_heartrate, max_heartrate, average_cadence, total_elevation_gain_m, pace_zone,
      lap_type (derived, rebuilt each sync — warmup/work/recovery/float/cooldown/steady;
      NULL for trivial laps under the 200m/45s floor)
      intensity (derived, rebuilt each sync — MP/threshold/interval/repetition/aerobic/
      sprint/sub-<zone> for work/float laps, judged against the fitness checkpoint for
      the month before the activity; NULL for other lap_types, trivial laps, or months
      with no reliable fitness estimate)

    Table: weather  (one row per activity; populated by miles-sync)
      activity_id, fetched_at, temp_c_start, temp_c_end, temp_c_avg, temp_c_max,
      apparent_temp_c_max, humidity_avg, precip_mm, wind_kph_avg, hourly_json

    Table: fitness_checkpoints  (derived, rebuilt each sync — not athlete-entered)
      month (YYYY-MM), confidence, source_tier (1=races, 2=workout laps, 3=envelope
      floor), pace_5k, pace_10k, pace_half, pace_marathon (decimal min/mi) — one row
      per calendar month with any fitness signal; see get_fitness_trend

    Table: meta  (key-value; derived-layer bookkeeping, rebuilt each sync)
      key, value — see derive_version, derived_at, last_sync_at (UTC ISO
      timestamp of the end of the most recent successful miles-sync, stamped
      by sync.py; get_training_plan/get_draft surface it pre-formatted as
      last_sync_at/days_stale rather than requiring a query here)

    Table: athlete  (single row, id = 1; athlete-entered, not derived)
      max_hr, long_run_floor_miles, updated_at — set via `miles-sync --max-hr` /
      `--long-run-floor`, or the one-time interactive prompt on first sync. Either
      field may be NULL if never set.

    Plan tables — athlete-authored ground truth, like activities: NOT derived,
    exempt from derive_all rebuilds. Write access is via start_plan_draft /
    start_revision_draft / set_draft_weeks / set_draft_days / delete_draft_weeks
    / delete_draft_days / commit_plan / discard_draft / log_plan_adjustment /
    abandon_plan (see those tools); read access to the current plan is better
    served by get_training_plan (committed versions) / get_draft (the mutable
    draft), but all six tables are queryable here for ad-hoc questions and
    history.

    Table: plans
      plan_id, title, race_date, distance_bucket, goal_time_s, status
      (draft|active|completed|abandoned), created_at. At most one row has
      status='active'; a draft plan may coexist with it (drafting next
      season's plan during a taper is normal — see start_plan_draft).

    Table: plan_versions  (append-only; there is no UPDATE path — a revision is
      always a brand new version_id with a full new set of weeks/days)
      version_id, plan_id, version_n, created_at, committed_at (NULL = this is
      the one mutable draft — its weeks/days may still change; a real
      timestamp = committed and immutable from then on. Contemporaneous
      scoring keys on committed_at, not created_at — a draft revision edited
      over several days takes effect from its commit date), note (why this
      revision — the athlete-approval record commit_plan requires),
      author (agent|manual)

    Table: plan_weeks  (PK: version_id, week_start)
      version_id, week_start (Monday), target_miles (range floor; point week =
      lo only, both target_miles and target_miles_hi NULL = deliberately
      unspecified, scored on workouts only), target_miles_hi (range upper
      bound, nullable), target_workouts, target_long_run_miles (nullable),
      target_long_run_minutes (nullable — duration long-run target alongside
      or instead of mileage), target_strength_days (nullable — counted per
      week, never banded or flagged), phase (base|sharpen|peak|taper|race),
      note (week-level athlete-facing guidance, e.g. phase context)

    Table: plan_days  (PK: version_id, date, seq; seq 1 normally, 2+ = doubles)
      version_id, date, seq, slot (easy|workout|long|rest|race|strength), title,
      target_miles (nullable), target_json (nullable — JSON-encoded DayTarget:
      reps/reps_lo/reps_hi (a rep range; reps stays the point form),
      rep_duration_s (time-based reps), rep_distance_m, pace_lo/pace_hi
      (decimal min/mi), zone_name, hr_lo/hr_hi, all optional; frozen at
      commit_plan time — never re-resolves on read; a draft may still hold a
      zone_name with no pace_lo/pace_hi, unresolved until commit), terrain
      (road|trail; NULL means road — composes with any run slot rather than
      being one itself, e.g. a trail long run is still a long run; a trail
      day's pace targets are display-guidance only, never scored), note
      (day-level athlete-facing guidance, e.g. "8-10 x 3min LT, go to 10 if
      feeling great" — distinct from plan_log's reality-annotations),
      target_minutes (nullable — duration target alongside or instead of
      target_miles)

    Table: plan_log  (day-level reality; doesn't touch the plan or bump a version)
      log_id, plan_id, date, action (skipped|moved|modified|note), reason, created_at

    Table: plan_adherence  (DERIVED — rebuilt from scratch by derive_all and
      versioned by DERIVE_VERSION like fitness_checkpoints; one row per
      completed week of the active plan; empty when there's no active plan
      or it hasn't reached a completed week yet. Prefer get_plan_adherence
      for normal reads — it also surfaces pattern flags.)
      plan_id, week_start, version_n_used (the plan version this week was
      scored against — see plan.current_version_for_week), actual_miles,
      actual_workouts, long_run_done (0/1/NULL — NULL means no long-run
      target that week, not a miss), mileage_ratio (actual/target),
      workout_pace_delta_s (seconds/mile outside the frozen workout pace
      range with +/-10s/mi slack; positive = slower, 0 = within range,
      NULL = no comparable workout data), band ('on'|'close'|'off'),
      flags_json (pattern-level flags active as of that week, JSON list or
      NULL — see get_plan_adherence for the semantics)
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
