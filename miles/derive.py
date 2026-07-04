"""
Full recompute of every value derived from raw synced rows (activities/laps).

Raw synced data is ground truth; everything here is rebuildable from it. `derive_all`
always recomputes from scratch — no incremental invalidation, ever — so a `git pull`
that changes a classifier or threshold only needs a rerun to take effect everywhere,
including ad-hoc `run_sql` queries.

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

from .classifier import classify_laps
from .db import effective_run_type_sql
from .fitness import (
    EFFORT_RACED_MAX,
    MILE_M,
    Confidence,
    classify_race_effort,
    estimate_fitness,
    hr_ceiling,
    hr_raced_fraction,
)
from .inference import apply_inference
from .races import NOMINAL_METERS, classify_race_distance

_CONFIDENCE_VALUES = get_args(Confidence)

# Bump whenever any classifier or threshold feeding a derived value changes, so
# ensure_derived() knows stale rows need a full recompute.
DERIVE_VERSION = "5"

# Checkpoint pace columns per race category — only these four are tracked; races in
# other buckets (15K, 10M, 30K, 50K) have no checkpoint prediction and are skipped.
_CHECKPOINT_PACE_COL: dict[str, str] = {
    "5K": "pace_5k", "10K": "pace_10k", "half": "pace_half", "marathon": "pace_marathon",
}

# Laps below this floor are never classified (too short/slow to read a speed signal).
LAP_MIN_DISTANCE_M = 200
LAP_MIN_MOVING_TIME_S = 45


def _type_laps(conn: sqlite3.Connection) -> int:
    """Recompute laps.lap_type for every activity that has laps. Returns count typed."""
    conn.execute("UPDATE laps SET lap_type = NULL")

    activity_ids = [
        int(row["activity_id"])
        for row in conn.execute("SELECT DISTINCT activity_id FROM laps").fetchall()
    ]

    typed = 0
    for activity_id in activity_ids:
        laps = conn.execute("""
            SELECT lap_id, distance_m, moving_time_s, average_speed_mps, average_heartrate
            FROM laps WHERE activity_id = ? ORDER BY lap_index
        """, [activity_id]).fetchall()

        classifiable = [
            i for i, lap in enumerate(laps)
            if lap["distance_m"] is not None and float(lap["distance_m"]) >= LAP_MIN_DISTANCE_M
            and lap["moving_time_s"] is not None and int(lap["moving_time_s"]) >= LAP_MIN_MOVING_TIME_S
            and lap["average_speed_mps"] is not None and float(lap["average_speed_mps"]) > 0
        ]
        if not classifiable:
            continue

        sub_types = classify_laps(
            speeds=[float(laps[i]["average_speed_mps"]) for i in classifiable],
            distances_m=[float(laps[i]["distance_m"]) for i in classifiable],
            heartrates=[
                float(laps[i]["average_heartrate"]) if laps[i]["average_heartrate"] is not None else None
                for i in classifiable
            ],
        )
        conn.executemany(
            "UPDATE laps SET lap_type = ? WHERE lap_id = ?",
            [(t, int(laps[i]["lap_id"])) for i, t in zip(classifiable, sub_types)],
        )
        typed += len(classifiable)

    return typed


# Confidence encodes the winning signal tier one-to-one.
_TIER_BY_CONFIDENCE: dict[str, int] = {"high": 1, "medium": 1, "medium-low": 2, "low": 3}


def _fitness_checkpoints(conn: sqlite3.Connection, *, exclude_casual: bool = False) -> int:
    """Rebuild the monthly fitness checkpoints from the first activity month
    through the current month. Returns rows inserted."""
    conn.execute("DELETE FROM fitness_checkpoints")

    row = conn.execute("SELECT MIN(DATE(start_date)) AS d FROM activities").fetchone()
    first: str | None = row["d"] if row is not None else None
    if first is None:
        return 0

    today = date.today()
    year, month = int(first[:4]), int(first[5:7])
    inserted = 0
    while (year, month) <= (today.year, today.month):
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        as_of = min(next_month - timedelta(days=1), today)
        est = estimate_fitness(conn, as_of, exclude_casual=exclude_casual)
        if est is not None:
            conn.execute("""
                INSERT INTO fitness_checkpoints
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


def _checkpoint_row(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    """Checkpoint row for `month`, falling back to the nearest earlier month with
    a row when that exact month has none."""
    row = conn.execute("""
        SELECT confidence, pace_5k, pace_10k, pace_half, pace_marathon
        FROM fitness_checkpoints WHERE month = ?
    """, [month]).fetchone()
    if row is not None:
        return row
    return conn.execute("""
        SELECT confidence, pace_5k, pace_10k, pace_half, pace_marathon
        FROM fitness_checkpoints WHERE month < ? ORDER BY month DESC LIMIT 1
    """, [month]).fetchone()


def _prior_month_str(year: int, month: int) -> str:
    """'YYYY-MM' for the calendar month immediately before (year, month)."""
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def _predicted_pace(conn: sqlite3.Connection, race_date: date, category: str) -> tuple[float, Confidence] | None:
    """(predicted pace min/mi, confidence) for a race's category, from the checkpoint
    of the month *before* race_date's month — never the activity's own month, which
    can include activities from later in that same month. Falls back to the nearest
    earlier month with a row; None when no checkpoint or category has no tracked column."""
    col = _CHECKPOINT_PACE_COL.get(category)
    if col is None:
        return None
    checkpoint = _checkpoint_row(conn, _prior_month_str(race_date.year, race_date.month))
    if checkpoint is None or checkpoint[col] is None:
        return None
    confidence_val = checkpoint["confidence"]
    assert confidence_val in _CONFIDENCE_VALUES, f"unexpected checkpoint confidence: {confidence_val!r}"
    return float(checkpoint[col]), cast(Confidence, confidence_val)


def _predicted_pace_for_race(conn: sqlite3.Connection, race_date: date, category: str) -> tuple[float, Confidence] | None:
    """(predicted pace min/mi, confidence) for a race's category, from a live fitness
    estimate as of the day before the race. Unlike a shared monthly checkpoint, this
    window can never include the race itself. None when the category has no tracked
    prediction or no estimate is computable that far back."""
    if category not in _CHECKPOINT_PACE_COL:
        return None
    est = estimate_fitness(conn, race_date - timedelta(days=1))
    if est is None:
        return None
    pace = est["predicted"].get(category)
    if pace is None:
        return None
    return pace, est["confidence"]


def _race_effort_pass(conn: sqlite3.Connection) -> dict[str, int]:
    """Classify race_effort/effort_ratio for every effective race with a computable
    pre-race estimate at its category. Clears both columns first — full recompute.
    Each race is judged against its own estimate_fitness(race_date - 1 day), not a
    shared checkpoint, so a race can never contaminate its own prediction."""
    conn.execute("UPDATE activities SET race_effort = NULL, effort_ratio = NULL")

    effective = effective_run_type_sql()
    rows = conn.execute(f"""
        SELECT activity_id, distance_m, moving_time_s, average_heartrate, DATE(start_date) AS date
        FROM activities WHERE {effective} = 'race'
    """).fetchall()

    counts: dict[str, int] = {}
    for r in rows:
        if r["moving_time_s"] is None:
            continue
        category = classify_race_distance(r["distance_m"])
        if category is None:
            continue
        race_date = date.fromisoformat(r["date"])
        predicted = _predicted_pace_for_race(conn, race_date, category)
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
    rows = conn.execute("""
        SELECT activity_id, distance_m, moving_time_s, average_heartrate, DATE(start_date) AS date
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


def derive_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Full recompute of every derived value from raw synced data. Never incremental."""
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

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('derive_version', ?)", (DERIVE_VERSION,)
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('derived_at', ?)", (now,)
    )
    conn.commit()

    return counts


def ensure_derived(conn: sqlite3.Connection) -> None:
    """Run derive_all if the DB's derived values are missing or from a stale version."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'derive_version'").fetchone()
    if row is None or row["value"] != DERIVE_VERSION:
        derive_all(conn)


def main() -> None:
    from . import db

    conn = db.connect()
    db.init_db(conn)
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")


if __name__ == "__main__":
    main()
