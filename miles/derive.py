"""
Recompute of every value derived from raw synced rows (activities/laps).

Raw synced data is ground truth; everything here is rebuildable from it. `derive_all(
full=True)` — used by the `miles-derive` CLI and forced automatically on a stale
DERIVE_VERSION — always recomputes from scratch, so a `git pull` that changes a
classifier or threshold only needs that rerun to take effect everywhere, including
ad-hoc `run_sql` queries. The default, incremental path scopes each pass to
meta.derive_dirty_since (see db.mark_derive_dirty and _derive_all_incremental's
docstring for why each pass's scoping cannot diverge from a full recompute).

One-pass contract: fitness checkpoints are computed twice. Race-effort classification
judges each race against its own estimate_fitness(as_of=race_date - 1 day) — never a
shared monthly checkpoint, which could span past the race and let it contaminate its
own prediction. Pass 1 (all races) instead seeds the pace-based race-inference step,
which reads the checkpoint of the month *before* an activity's month; pass 2 (excluding
casual-effort races) is the final, refined checkpoint table other tools read. Neither
pass's checkpoints are fed back into effort classification.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import cast, get_args

from . import db
from .classifier import classify_laps
from .db import effective_run_type_sql, get_meta, hr_sql, set_meta
from .fitness import (
    EFFORT_RACED_MAX,
    MILE_M,
    PREDICTED_KEYS,
    Confidence,
    classify_race_effort,
    estimate_fitness,
    hr_ceiling,
    hr_raced_fraction,
    intensity_for_pace,
    zones_from_predicted,
)
from .inference import apply_inference
from .plan_adherence import compute_plan_adherence
from .races import NOMINAL_METERS, classify_race_distance

_CONFIDENCE_VALUES = get_args(Confidence)

# Bump whenever any classifier or threshold feeding a derived value changes, so
# ensure_derived() knows stale rows need a full recompute.
DERIVE_VERSION = "12"

_FINAL_CHECKPOINTS = "fitness_checkpoints"
_SEED_CHECKPOINTS = "fitness_checkpoints_seed"

# A single intensity must hold at least this share of a session's work-lap moving
# time to count as the session's dominant_intensity; below it the session is mixed.
DOMINANT_INTENSITY_MIN_SHARE = 0.60

# Checkpoint pace columns per race category — only these four are tracked; races in
# other buckets (15K, 10M, 30K, 50K) have no checkpoint prediction and are skipped.
CHECKPOINT_PACE_COL: dict[str, str] = {k: f"pace_{k.lower()}" for k in PREDICTED_KEYS}


def _type_laps(conn: sqlite3.Connection, *, since: str | None = None) -> int:
    """Recompute laps.lap_type for activities that have laps, scoped to DATE(start_date)
    >= since when given. Each activity's laps classify independently of every other
    activity's (classify_laps takes one activity's laps at a time), so this scoping
    can never diverge from a full recompute for an activity dated before since.
    Returns count typed."""
    if since is None:
        conn.execute("UPDATE laps SET lap_type = NULL")
        activity_ids = [
            int(row["activity_id"])
            for row in conn.execute("SELECT DISTINCT activity_id FROM laps").fetchall()
        ]
    else:
        conn.execute("""
            UPDATE laps SET lap_type = NULL
            WHERE activity_id IN (SELECT activity_id FROM activities WHERE DATE(start_date) >= ?)
        """, [since])
        activity_ids = [
            int(row["activity_id"])
            for row in conn.execute("""
                SELECT DISTINCT l.activity_id FROM laps l
                JOIN activities a ON a.activity_id = l.activity_id
                WHERE DATE(a.start_date) >= ?
            """, [since]).fetchall()
        ]

    typed = 0
    for activity_id in activity_ids:
        laps = conn.execute(f"""
            SELECT l.lap_id, l.distance_m, l.moving_time_s, l.average_speed_mps,
                   {hr_sql("l.average_heartrate", "a")} AS average_heartrate
            FROM laps l JOIN activities a ON a.activity_id = l.activity_id
            WHERE l.activity_id = ? ORDER BY l.lap_index
        """, [activity_id]).fetchall()

        # classify_laps sees every lap (not just floor-passing ones) so it can
        # backfill sub-floor between-rep rests; missing distance/time/speed values
        # coerce to 0.0, which never passes the floor, matching prior behavior.
        types = classify_laps(
            speeds=[float(lap["average_speed_mps"] or 0.0) for lap in laps],
            distances_m=[float(lap["distance_m"] or 0.0) for lap in laps],
            heartrates=[
                float(lap["average_heartrate"]) if lap["average_heartrate"] is not None else None
                for lap in laps
            ],
            moving_times_s=[float(lap["moving_time_s"] or 0.0) for lap in laps],
        )
        conn.executemany(
            "UPDATE laps SET lap_type = ? WHERE lap_id = ?",
            [(t, int(lap["lap_id"])) for lap, t in zip(laps, types) if t is not None],
        )
        typed += sum(1 for t in types if t is not None)

    return typed


# Confidence encodes the winning signal tier one-to-one.
_TIER_BY_CONFIDENCE: dict[str, int] = {"high": 1, "medium": 1, "medium-low": 2, "low": 3}


def _fitness_checkpoints(
    conn: sqlite3.Connection, *, exclude_casual: bool = False, since_month: tuple[int, int] | None = None
) -> int:
    """Rebuild monthly fitness checkpoints: the final table when exclude_casual, else
    the seed table (see db.init_db). since_month=None rebuilds every month from
    the first activity through the current month; a (year, month) scopes the rebuild
    to months >= that one. Safe to scope because a checkpoint's as_of value depends
    only on activities on or before it (fitness.py's tier1/2/3 windows all end at
    as_of — see estimate_fitness), so a month before since_month can never depend on
    anything at or after it and is left untouched. Returns rows inserted."""
    table = _FINAL_CHECKPOINTS if exclude_casual else _SEED_CHECKPOINTS
    if since_month is None:
        conn.execute(f"DELETE FROM {table}")
        row = conn.execute("SELECT MIN(DATE(start_date)) AS d FROM activities").fetchone()
        first: str | None = row["d"] if row is not None else None
        if first is None:
            return 0
        year, month = int(first[:4]), int(first[5:7])
    else:
        year, month = since_month
        conn.execute(f"DELETE FROM {table} WHERE month >= ?", [f"{year:04d}-{month:02d}"])

    today = date.today()
    inserted = 0
    while (year, month) <= (today.year, today.month):
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        as_of = min(next_month - timedelta(days=1), today)
        est = estimate_fitness(conn, as_of, exclude_casual=exclude_casual)
        if est is not None:
            conn.execute(f"""
                INSERT INTO {table}
                    (month, confidence, source_tier, pace_5k, pace_10k, pace_half, pace_marathon)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                f"{year:04d}-{month:02d}",
                est["confidence"],
                _TIER_BY_CONFIDENCE[est["confidence"]],
                est["predicted"]["5K"],
                est["predicted"]["10K"],
                est["predicted"]["half"],
                est["predicted"]["marathon"],
            ))
            inserted += 1
        year, month = next_month.year, next_month.month

    return inserted


def _checkpoint_row(conn: sqlite3.Connection, month: str, table: str = _FINAL_CHECKPOINTS) -> sqlite3.Row | None:
    """Checkpoint row for `month`, falling back to the nearest earlier month with
    a row when that exact month has none."""
    row = conn.execute(f"""
        SELECT confidence, pace_5k, pace_10k, pace_half, pace_marathon
        FROM {table} WHERE month = ?
    """, [month]).fetchone()
    if row is not None:
        return row
    return conn.execute(f"""
        SELECT confidence, pace_5k, pace_10k, pace_half, pace_marathon
        FROM {table} WHERE month < ? ORDER BY month DESC LIMIT 1
    """, [month]).fetchone()


def _prior_month_str(year: int, month: int) -> str:
    """'YYYY-MM' for the calendar month immediately before (year, month)."""
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _predicted_pace(conn: sqlite3.Connection, race_date: date, category: str) -> tuple[float, Confidence] | None:
    """(predicted pace min/mi, confidence) for a race's category, from the seed
    checkpoint of the month *before* race_date's month — never the activity's own
    month, which can include activities from later in that same month. Falls back to
    the nearest earlier month with a row; None when no checkpoint or category has no
    tracked column."""
    col = CHECKPOINT_PACE_COL.get(category)
    if col is None:
        return None
    checkpoint = _checkpoint_row(conn, _prior_month_str(race_date.year, race_date.month), _SEED_CHECKPOINTS)
    if checkpoint is None or checkpoint[col] is None:
        return None
    confidence_val = checkpoint["confidence"]
    assert confidence_val in _CONFIDENCE_VALUES, f"unexpected checkpoint confidence: {confidence_val!r}"
    return float(checkpoint[col]), cast(Confidence, confidence_val)


def predicted_pace_for_race(conn: sqlite3.Connection, race_date: date, category: str) -> tuple[float, Confidence] | None:
    """(predicted pace min/mi, confidence) for a race's category, from a live fitness
    estimate as of the day before the race. Unlike a shared monthly checkpoint, this
    window can never include the race itself. None when the category has no tracked
    prediction or no estimate is computable that far back."""
    if category not in CHECKPOINT_PACE_COL:
        return None
    est = estimate_fitness(conn, race_date - timedelta(days=1))
    if est is None:
        return None
    pace = est["predicted"].get(category)
    if pace is None:
        return None
    return pace, est["confidence"]


def _race_effort_pass(conn: sqlite3.Connection, *, since: str | None = None) -> dict[str, int]:
    """Classify race_effort/effort_ratio for every effective race with a computable
    pre-race estimate at its category, scoped to DATE(start_date) >= since when given.
    Each race is judged against its own estimate_fitness(race_date - 1 day) — a live,
    backward-only window that ends before the race itself — so a race dated before
    since can never see anything at or after since, and is safe to leave untouched."""
    if since is None:
        conn.execute("UPDATE activities SET race_effort = NULL, effort_ratio = NULL")
    else:
        conn.execute(
            "UPDATE activities SET race_effort = NULL, effort_ratio = NULL WHERE DATE(start_date) >= ?",
            [since],
        )

    effective = effective_run_type_sql()
    query = f"""
        SELECT activity_id, distance_m, moving_time_s, {hr_sql("average_heartrate")} AS average_heartrate,
               DATE(start_date) AS date
        FROM activities WHERE {effective} = 'race'
    """
    params: list[str] = []
    if since is not None:
        query += " AND DATE(start_date) >= ?"
        params.append(since)
    rows = conn.execute(query, params).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        if r["moving_time_s"] is None:
            continue
        category = classify_race_distance(r["distance_m"])
        if category is None:
            continue
        race_date = date.fromisoformat(r["date"])
        predicted = predicted_pace_for_race(conn, race_date, category)
        if predicted is None:
            continue
        predicted_pace, confidence = predicted

        finish_s = float(r["moving_time_s"])
        actual_pace = (finish_s / 60.0) / (NOMINAL_METERS[category] / MILE_M)
        avg_hr = float(r["average_heartrate"]) if r["average_heartrate"] is not None else None
        ceiling = hr_ceiling(conn, race_date)
        effort, ratio = classify_race_effort(actual_pace, predicted_pace, avg_hr, ceiling, confidence, category)

        conn.execute(
            "UPDATE activities SET race_effort = ?, effort_ratio = ? WHERE activity_id = ?",
            (effort, ratio, int(r["activity_id"])),
        )
        counts[effort] = counts.get(effort, 0) + 1
    return counts


def _pace_based_race_inference(conn: sqlite3.Connection) -> int:
    """Untagged, unnamed races the name-based pass (step 1) missed: no name signal
    required, just a checkpoint-fast pace corroborated by near-max HR. Sets
    run_type_inferred='race' and persists effort columns inline. Returns count."""
    rows = conn.execute(f"""
        SELECT activity_id, distance_m, moving_time_s, {hr_sql("average_heartrate")} AS average_heartrate,
               DATE(start_date) AS date
        FROM activities WHERE workout_type = 0 AND run_type_inferred IS NULL
    """).fetchall()

    count = 0
    for r in rows:
        if r["moving_time_s"] is None:
            continue
        category = classify_race_distance(r["distance_m"])
        if category is None:
            continue
        race_date = date.fromisoformat(r["date"])
        predicted = _predicted_pace(conn, race_date, category)
        if predicted is None:
            continue
        predicted_pace, confidence = predicted

        avg_hr = float(r["average_heartrate"]) if r["average_heartrate"] is not None else None
        ceiling = hr_ceiling(conn, race_date)
        if avg_hr is None or ceiling is None:
            continue

        finish_s = float(r["moving_time_s"])
        actual_pace = (finish_s / 60.0) / (NOMINAL_METERS[category] / MILE_M)
        ratio = actual_pace / predicted_pace
        if ratio > EFFORT_RACED_MAX or avg_hr < hr_raced_fraction(category) * ceiling:
            continue

        effort, _ = classify_race_effort(actual_pace, predicted_pace, avg_hr, ceiling, confidence, category)
        conn.execute("""
            UPDATE activities SET run_type_inferred = 'race', race_effort = ?, effort_ratio = ?
            WHERE activity_id = ?
        """, (effort, ratio, int(r["activity_id"])))
        count += 1
    return count


def _lap_intensity_pass(conn: sqlite3.Connection, *, since: str | None = None) -> dict[str, int]:
    """Tag work/float laps with intensity (MP/threshold/interval/repetition/
    aerobic/sprint/sub-*) using the zone anchors from the fitness checkpoint of
    the month *before* each activity's month — never that activity's own month,
    which could include itself. Skips activities with no earlier checkpoint or a
    low-confidence one: a wrong label is worse than none. Rolls each session's
    work-lap intensities up into dominant_intensity when one intensity holds
    >= DOMINANT_INTENSITY_MIN_SHARE of work-lap moving time.

    Scoped to DATE(start_date) >= since when given: each activity only reads a
    checkpoint from *before* its own month, and checkpoint months before since are
    left untouched by _fitness_checkpoints, so an activity dated before since always
    sees the same checkpoint and computes the same intensities either way."""
    if since is None:
        conn.execute("UPDATE laps SET intensity = NULL")
        conn.execute("UPDATE activities SET dominant_intensity = NULL")
        activity_ids = [
            int(row["activity_id"])
            for row in conn.execute("""
                SELECT DISTINCT activity_id FROM laps WHERE lap_type IN ('work', 'float')
            """).fetchall()
        ]
    else:
        conn.execute("""
            UPDATE laps SET intensity = NULL
            WHERE activity_id IN (SELECT activity_id FROM activities WHERE DATE(start_date) >= ?)
        """, [since])
        conn.execute(
            "UPDATE activities SET dominant_intensity = NULL WHERE DATE(start_date) >= ?", [since]
        )
        activity_ids = [
            int(row["activity_id"])
            for row in conn.execute("""
                SELECT DISTINCT l.activity_id FROM laps l
                JOIN activities a ON a.activity_id = l.activity_id
                WHERE l.lap_type IN ('work', 'float') AND DATE(a.start_date) >= ?
            """, [since]).fetchall()
        ]

    laps_intensity = 0
    sessions_dominant = 0
    for activity_id in activity_ids:
        row = conn.execute(
            "SELECT DATE(start_date) AS date FROM activities WHERE activity_id = ?", [activity_id]
        ).fetchone()
        if row is None or row["date"] is None:
            continue
        activity_date = date.fromisoformat(row["date"])
        checkpoint = _checkpoint_row(conn, _prior_month_str(activity_date.year, activity_date.month))
        if checkpoint is None or checkpoint["confidence"] == "low":
            continue
        zones = zones_from_predicted(float(checkpoint["pace_marathon"]), float(checkpoint["pace_5k"]))

        laps = conn.execute("""
            SELECT lap_id, lap_index, lap_type, moving_time_s, average_speed_mps
            FROM laps
            WHERE activity_id = ? AND lap_type IN ('work', 'float')
              AND average_speed_mps IS NOT NULL AND average_speed_mps > 0
            ORDER BY lap_index
        """, [activity_id]).fetchall()
        if not laps:
            continue

        work_time_by_intensity: dict[str, int] = {}
        total_work_s = 0
        for lap in laps:
            pace = 26.8224 / float(lap["average_speed_mps"])
            intensity = intensity_for_pace(pace, zones)
            conn.execute("UPDATE laps SET intensity = ? WHERE lap_id = ?", (intensity, int(lap["lap_id"])))
            if intensity is None:
                continue
            laps_intensity += 1
            if lap["lap_type"] == "work":
                t = int(lap["moving_time_s"] or 0)
                work_time_by_intensity[intensity] = work_time_by_intensity.get(intensity, 0) + t
                total_work_s += t

        if total_work_s > 0:
            dom_intensity, dom_s = max(work_time_by_intensity.items(), key=lambda kv: kv[1])
            if dom_s / total_work_s >= DOMINANT_INTENSITY_MIN_SHARE:
                conn.execute(
                    "UPDATE activities SET dominant_intensity = ? WHERE activity_id = ?",
                    (dom_intensity, activity_id),
                )
                sessions_dominant += 1

    return {"laps_intensity": laps_intensity, "sessions_dominant": sessions_dominant}


def _plan_adherence_pass(conn: sqlite3.Connection) -> dict[str, int]:
    """Full recompute of plan_adherence: every sync-covered week (see
    plan_adherence.py's unsynced-week guard) of the active plan plus every
    completed plan (see plan_adherence.py's _target_plans, which keeps a
    finished plan's final adherence numbers alive once the athlete starts
    the next one), scored against the plan version that governed it at the
    time. DELETE-then-rebuild, like every other pass here. A no-op — deletes
    whatever was there, inserts nothing — when there's no active or
    completed plan, which is the real DB's current state; plan tables are
    athlete-authored ground truth, exempt from this rebuild themselves (only
    plan_adherence is derived from them)."""
    conn.execute("DELETE FROM plan_adherence")
    rows = compute_plan_adherence(conn)
    if rows:
        conn.executemany("""
            INSERT INTO plan_adherence (
                plan_id, week_start, version_n_used, actual_miles, actual_workouts,
                actual_strength_days, long_run_done, mileage_ratio, workout_pace_delta_s,
                band, flags_json
            ) VALUES (
                :plan_id, :week_start, :version_n_used, :actual_miles, :actual_workouts,
                :actual_strength_days, :long_run_done, :mileage_ratio, :workout_pace_delta_s,
                :band, :flags_json
            )
        """, rows)
    return {"plan_adherence_weeks": len(rows)}


def _dirty_since(conn: sqlite3.Connection) -> str | None:
    return get_meta(conn, "derive_dirty_since")


def _stamp_version(conn: sqlite3.Connection) -> None:
    set_meta(conn, "derive_version", DERIVE_VERSION)
    set_meta(conn, "derived_at", datetime.now(timezone.utc).isoformat())
    set_meta(conn, "derived_month", date.today().isoformat()[:7])


def _month_of(iso_date: str) -> tuple[int, int]:
    return int(iso_date[:4]), int(iso_date[5:7])


def _first_stale_month(conn: sqlite3.Connection, since_month: tuple[int, int]) -> tuple[int, int]:
    """Earliest checkpoint month an incremental run must rebuild: since_month, or
    the month of the last derive when that is earlier, because that month's
    checkpoint was cut off at the day of the last derive and has since grown."""
    last = get_meta(conn, "derived_month")
    return min(since_month, _month_of(last)) if last else since_month


def _derive_all_full(conn: sqlite3.Connection) -> dict[str, int]:
    """Complete rebuild of every derived value from raw synced data, ignoring any
    dirty-tracking state. Required after a classifier/threshold change (DERIVE_VERSION
    bump) and used unconditionally by the `miles-derive` CLI."""
    counts: dict[str, int] = {}

    inferred = apply_inference(conn)
    for run_type, n in inferred.items():
        counts[f"inferred_{run_type}"] = n

    counts["laps_typed"] = _type_laps(conn)

    _fitness_checkpoints(conn)  # pass 1: seeds the pace-based race-inference step below

    effort_counts = _race_effort_pass(conn)
    for effort, n in effort_counts.items():
        counts[f"race_effort_{effort}"] = n

    pace_inferred = _pace_based_race_inference(conn)
    counts["race_pace_inferred"] = pace_inferred
    if pace_inferred:
        counts["inferred_race"] = counts.get("inferred_race", 0) + pace_inferred

    counts["fitness_months"] = _fitness_checkpoints(conn, exclude_casual=True)  # pass 2, final

    counts.update(_lap_intensity_pass(conn))

    counts.update(_plan_adherence_pass(conn))

    conn.execute("DELETE FROM meta WHERE key = 'derive_dirty_since'")
    _stamp_version(conn)
    conn.commit()

    return counts


def _derive_all_incremental(conn: sqlite3.Connection, since: str) -> dict[str, int]:
    """Recompute only what activities dated on or after `since` could have changed.

    _type_laps, _race_effort_pass and _lap_intensity_pass are scoped to DATE(start_date)
    >= since and provably match a full recompute for anything dated earlier: each
    classifies one activity from a trailing window that ends at or before that
    activity's own date (see their docstrings), so nothing at or after `since` can
    ever feed back into an earlier activity's derived values. _fitness_checkpoints is
    scoped by calendar month on the same reasoning (see its docstring).

    apply_inference and _pace_based_race_inference are the exception — both always run
    in full. apply_inference unconditionally resets and re-derives run_type_inferred
    for every workout_type=0 row on every call (cheap, ~0.4s); _pace_based_race_inference
    then fills in whatever apply_inference left NULL, including activities pace-inferred
    as races on a prior run. Both are pure functions of an activity's own backward-
    looking history, so they reproduce identical results for pre-`since` activities
    every time — but only because they run over the whole table: scoping either one
    to `since` would leave old pace-inferred races wiped by apply_inference's reset
    and never refilled.
    """
    counts: dict[str, int] = {}

    inferred = apply_inference(conn)
    for run_type, n in inferred.items():
        counts[f"inferred_{run_type}"] = n

    counts["laps_typed"] = _type_laps(conn, since=since)

    since_month = _first_stale_month(conn, _month_of(since))
    _fitness_checkpoints(conn, since_month=since_month)  # pass 1

    effort_counts = _race_effort_pass(conn, since=since)
    for effort, n in effort_counts.items():
        counts[f"race_effort_{effort}"] = n

    pace_inferred = _pace_based_race_inference(conn)
    counts["race_pace_inferred"] = pace_inferred
    if pace_inferred:
        counts["inferred_race"] = counts.get("inferred_race", 0) + pace_inferred

    counts["fitness_months"] = _fitness_checkpoints(conn, exclude_casual=True, since_month=since_month)  # pass 2, final

    counts.update(_lap_intensity_pass(conn, since=since))

    counts.update(_plan_adherence_pass(conn))

    conn.execute("DELETE FROM meta WHERE key = 'derive_dirty_since'")
    _stamp_version(conn)
    conn.commit()

    return counts


def _derive_all_untouched(conn: sqlite3.Connection) -> dict[str, int]:
    """Nothing has synced since the last derive. plan_adherence is athlete-edited
    outside of sync (plans can change without a new activity), and a fitness
    checkpoint's as_of for the current month is `today` — which moves even with no
    new activities — so both get recomputed; everything else is unchanged from the
    last run."""
    counts: dict[str, int] = {}
    since_month = _first_stale_month(conn, _month_of(date.today().isoformat()))
    _fitness_checkpoints(conn, since_month=since_month)
    counts["fitness_months"] = _fitness_checkpoints(conn, exclude_casual=True, since_month=since_month)
    counts.update(_plan_adherence_pass(conn))
    _stamp_version(conn)
    conn.commit()
    return counts


def derive_all(conn: sqlite3.Connection, *, full: bool = False) -> dict[str, int]:
    """Recompute derived values from raw synced rows (activities/laps). Raw synced
    data is ground truth; everything here is rebuildable from it.

    full=True, or a stale DERIVE_VERSION, forces a complete rebuild — the only path
    guaranteed correct after a classifier/threshold change. Otherwise this recomputes
    only what meta.derive_dirty_since (set by db.mark_derive_dirty, lowered by every
    raw-data writer) says could have changed, which for a sync adding a handful of
    recent activities is the difference between a full rebuild and a sub-second pass.
    """
    if full or get_meta(conn, "derive_version") != DERIVE_VERSION:
        return _derive_all_full(conn)

    since = _dirty_since(conn)
    if since is None:
        return _derive_all_untouched(conn)
    return _derive_all_incremental(conn, since)


def ensure_derived(conn: sqlite3.Connection) -> None:
    """Run derive_all if the DB's derived values are missing or from a stale version."""
    if get_meta(conn, "derive_version") != DERIVE_VERSION:
        derive_all(conn, full=True)


def connect_derived() -> sqlite3.Connection:
    """A connection whose derived values are current."""
    conn = db.connect()
    ensure_derived(conn)
    return conn


def main() -> None:
    conn = db.connect()
    db.init_db(conn)
    counts = derive_all(conn, full=True)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")


if __name__ == "__main__":
    main()
