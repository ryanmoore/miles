import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import click
from stravalib.exc import Fault

from . import db, plan, strava_client, weather as weather_module
from .classifier import classify_workout
from .derive import derive_all


def _wait_for_rate_limit() -> None:
    now = datetime.now()
    seconds_into_window = (now.minute % 15) * 60 + now.second
    wait = (15 * 60) - seconds_into_window + 5  # +5s buffer past window boundary
    print(f"\nRate limit hit — sleeping {wait}s until next 15-min window...", flush=True)
    time.sleep(wait)


def _extra_lap_backfill(conn: sqlite3.Connection, extra_limit: int, reserve: int) -> None:
    """Backfill laps for all remaining runs, long runs first then newest-first.
    Resumable: each activity is stamped and committed individually, and a fresh
    generator is built for the remaining ids after a rate-limit interruption.
    """
    # The incremental sync earlier in _run has already hit the API, so the
    # recorded daily usage is current — skip without spending a single call
    # when the reserve is already gone (e.g. a second --extra run today).
    remaining_calls = strava_client.daily_calls_remaining()
    if remaining_calls is not None and remaining_calls <= reserve:
        print(
            f"Skipping --extra backfill: {remaining_calls} daily API calls left "
            f"(reserving {reserve} for later syncs); rerun tomorrow."
        )
        return

    effective_run_type = db.effective_run_type_sql()
    remaining_ids = [
        row["activity_id"]
        for row in conn.execute(f"""
            SELECT activity_id FROM activities
            WHERE sport_type IN ('Run', 'TrailRun', 'VirtualRun')
              AND laps_synced_at IS NULL
            ORDER BY CASE WHEN {effective_run_type} = 'long_run' THEN 0 ELSE 1 END,
                     start_date DESC
        """).fetchall()
    ]
    total_remaining = len(remaining_ids)
    print(f"Extra backfill: {total_remaining} remaining, fetching up to {extra_limit} this run...")

    todo_ids = remaining_ids[:extra_limit]
    batch_size = len(todo_ids)
    fetched = 0
    lap_total = 0
    # Nonzero start: the first 429 always waits out the 15-min window; only a
    # 429 after a fruitless full-window wait means the daily cap.
    successes_since_429 = 1

    stopped_for_reserve = False
    while todo_ids and not stopped_for_reserve:
        processed_in_attempt = 0
        try:
            for activity_id, laps in strava_client.get_activity_laps_batch(todo_ids):
                if laps:
                    db.upsert_laps(conn, laps)
                    lap_total += len(laps)
                conn.execute(
                    "UPDATE activities SET laps_synced_at = datetime('now') WHERE activity_id = ?",
                    (activity_id,),
                )
                conn.commit()
                fetched += 1
                successes_since_429 += 1
                processed_in_attempt += 1
                if fetched % 25 == 0:
                    print(f"  {fetched}/{batch_size} fetched, {lap_total} laps...")
                remaining_calls = strava_client.daily_calls_remaining()
                if remaining_calls is not None and remaining_calls <= reserve:
                    remaining_after = total_remaining - fetched
                    print(
                        f"Stopping --extra backfill: {remaining_calls} daily API calls left "
                        f"(reserving {reserve} for later syncs) — {remaining_after} activities "
                        "still unsynced; rerun tomorrow."
                    )
                    stopped_for_reserve = True
                    break
            else:
                todo_ids = []
        except Fault as e:
            if e.response is not None and e.response.status_code == 429:
                todo_ids = todo_ids[processed_in_attempt:]
                if successes_since_429 == 0:
                    remaining_after = total_remaining - fetched
                    print(
                        f"Daily API limit reached — {remaining_after} activities left; "
                        "rerun 'miles-sync --extra' tomorrow."
                    )
                    break
                _wait_for_rate_limit()
                successes_since_429 = 0
            else:
                raise

    remaining_after = total_remaining - fetched
    print(f"Extra backfill: {fetched} fetched this run, {remaining_after} remaining.")


def _run(
    conn: sqlite3.Connection,
    full: bool,
    extra: bool,
    extra_limit: int = 900,
    extra_reserve: int = 10,
) -> None:
    after = None if full else db.last_synced_date(conn)
    if after:
        print(f"Incremental sync: fetching activities after {after}")
    else:
        print("Full sync: fetching all activities (may take a few minutes)...")

    rows = []
    for i, row in enumerate(strava_client.get_activities(after_ts=after)):
        rows.append(row)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1} activities fetched...")

    if rows:
        db.upsert_activities(conn, rows)

    total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    print(f"Done. {len(rows)} new/updated. {total} total in DB.")

    # Run derive here (not just at the end) so newly synced rows have run_type_inferred
    # populated before the lap fetch below queries effective type — otherwise
    # freshly-inferred races/workouts would be skipped for another sync cycle.
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")

    # Auto-complete the active plan when a synced effective-race activity
    # now matches its race_date (+/-1 day) and distance_bucket (case-
    # insensitive) — placed after the derive_all above so a race that was
    # only just inferred (run_type_inferred) this same sync cycle is already
    # visible to the match, not just on the next sync. The only mutation is
    # the status flip; plans/plan_versions/plan_weeks/plan_days stay
    # untouched and append-only. No-ops cleanly with no active plan or no
    # matching race (see plan.auto_complete_plan).
    completed_plan_id = plan.auto_complete_plan(conn)
    if completed_plan_id is not None:
        print(f"Plan {completed_plan_id} auto-completed (matching race synced).")

    # Lazy lap sync: fetch laps for workout/race activities that have none yet.
    # Backstamp anything already fetched by a prior sync so it's never refetched.
    conn.execute("""
        UPDATE activities SET laps_synced_at = datetime('now')
        WHERE laps_synced_at IS NULL
          AND activity_id IN (SELECT DISTINCT activity_id FROM laps)
    """)
    conn.commit()

    effective_run_type = db.effective_run_type_sql()
    unsynced = conn.execute(f"""
        SELECT activity_id, name FROM activities
        WHERE {effective_run_type} IN ('workout', 'race')
          AND laps_synced_at IS NULL
        ORDER BY start_date
    """).fetchall()

    if unsynced:
        total_workouts = len(unsynced)
        print(f"Fetching laps for {total_workouts} workout(s)...")
        ids = [a["activity_id"] for a in unsynced]
        names = {a["activity_id"]: a["name"] for a in unsynced}
        lap_total = 0
        for i, (activity_id, laps) in enumerate(strava_client.get_activity_laps_batch(ids), 1):
            if laps:
                db.upsert_laps(conn, laps)
                lap_total += len(laps)
            conn.execute(
                "UPDATE activities SET laps_synced_at = datetime('now') WHERE activity_id = ?",
                (activity_id,),
            )
            conn.commit()
            name: str | None = names.get(activity_id)
            if name:
                label = classify_workout(name)
                if label:
                    conn.execute(
                        "UPDATE activities SET workout_label = ? WHERE activity_id = ? AND workout_label IS NULL",
                        (label, activity_id),
                    )
                    conn.commit()
            if i % 5 == 0 or i == total_workouts:
                print(f"  {i}/{total_workouts} workouts, {lap_total} laps...")
        print(f"Laps done. {lap_total} total.")

    # Backfill labels for any workout activities that have laps but no label yet.
    unlabeled = conn.execute("""
        SELECT activity_id, name FROM activities
        WHERE run_type = 'workout' AND workout_label IS NULL
          AND activity_id IN (SELECT DISTINCT activity_id FROM laps)
    """).fetchall()
    for activity in unlabeled:
        unlabeled_name: str | None = activity["name"]
        if unlabeled_name:
            label = classify_workout(unlabeled_name)
            if label:
                conn.execute(
                    "UPDATE activities SET workout_label = ? WHERE activity_id = ?",
                    (label, activity["activity_id"]),
                )
    if unlabeled:
        conn.commit()

    # Weather sync: fetch for any activity with location but no weather yet.
    needs_weather = conn.execute("""
        SELECT a.activity_id, a.start_lat, a.start_lng, a.start_date, a.moving_time_s
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.start_lat IS NOT NULL AND a.start_lng IS NOT NULL
          AND a.moving_time_s IS NOT NULL AND a.start_date IS NOT NULL
          AND w.activity_id IS NULL
        ORDER BY a.start_date DESC
    """).fetchall()

    if needs_weather:
        total_w = len(needs_weather)
        print(f"Fetching weather for {total_w} activities...")

        # Group by rounded location (~11km grid) so each group needs at most 2 API calls.
        groups: defaultdict[tuple[float, float], list[weather_module.WeatherSpec]] = defaultdict(list)
        for act in needs_weather:
            key = (round(float(act["start_lat"]), 1), round(float(act["start_lng"]), 1))
            start_dt = datetime.fromisoformat(act["start_date"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            groups[key].append({
                "activity_id": act["activity_id"],
                "start_dt": start_dt,
                "duration_s": int(act["moving_time_s"]),
            })

        total_groups = len(groups)
        print(f"  {total_groups} location group(s) — at most {total_groups * 2} API calls total.")
        fetched_w = 0
        for g_idx, ((lat, lng), specs) in enumerate(groups.items(), 1):
            rows = weather_module.fetch_weather_bulk(specs, lat, lng)
            if rows:
                db.upsert_weather(conn, rows)
                fetched_w += len(rows)
            print(f"  Group {g_idx}/{total_groups} ({lat:.1f},{lng:.1f}): {len(rows)}/{len(specs)} fetched. Total: {fetched_w}/{total_w}")

        print(f"Weather done. {fetched_w} new records.")

    if extra:
        _extra_lap_backfill(conn, extra_limit, extra_reserve)

    # Recompute all derived values (inferred run types, lap types, ...) from raw synced rows.
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")

    # Stamp the moment this sync finished so readers (adherence, plan tools)
    # can tell how fresh the synced data is without guessing from activities.
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_sync_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [datetime.now(timezone.utc).isoformat()],
    )
    conn.commit()


@click.command()
@click.option("--full", is_flag=True, help="Ignore last sync date and fetch everything.")
@click.option("--extra", is_flag=True, help="Backfill laps for all remaining runs, most important first (resumable; rerun daily until complete).")
@click.option("--extra-limit", type=int, default=900, help="Max lap fetches per --extra invocation.")
@click.option("--extra-reserve", type=int, default=10, help="Daily API calls to leave unused when --extra backfills (so later syncs today can still fetch new activities).")
@click.option("--max-hr", type=int, default=None, help="Set max heart rate and exit (no Strava calls).")
@click.option("--long-run-floor", type=float, default=None, help="Set long-run distance floor in miles and exit (no Strava calls).")
def main(
    full: bool,
    extra: bool,
    extra_limit: int,
    extra_reserve: int,
    max_hr: int | None,
    long_run_floor: float | None,
) -> None:
    conn = db.connect()
    db.init_db(conn)

    if max_hr is not None or long_run_floor is not None:
        existing = db.get_athlete(conn)
        merged_max_hr = max_hr if max_hr is not None else (existing["max_hr"] if existing else None)
        merged_floor = (
            long_run_floor if long_run_floor is not None
            else (existing["long_run_floor_miles"] if existing else None)
        )
        db.upsert_athlete(conn, max_hr=merged_max_hr, long_run_floor_miles=merged_floor)
        counts = derive_all(conn)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
        print(f"Athlete profile updated. Derive done. {summary}")
        return

    if db.get_athlete(conn) is None and sys.stdin.isatty():
        raw = click.prompt(
            "Max heart rate for HR-based analysis (Enter to skip)",
            default="", show_default=False,
        )
        prompted_max_hr: int | None
        try:
            prompted_max_hr = int(raw) if raw.strip() else None
        except ValueError:
            prompted_max_hr = None
        db.upsert_athlete(conn, max_hr=prompted_max_hr, long_run_floor_miles=None)

    while True:
        try:
            _run(conn, full, extra, extra_limit, extra_reserve)
            break
        except Fault as e:
            if e.response is not None and e.response.status_code == 429:
                _wait_for_rate_limit()
            else:
                raise


if __name__ == "__main__":
    main()
