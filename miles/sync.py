import sqlite3
import time
from datetime import datetime

import click
from stravalib.exc import Fault

from . import db, strava_client
from .classifier import classify_workout


def _wait_for_rate_limit() -> None:
    now = datetime.now()
    seconds_into_window = (now.minute % 15) * 60 + now.second
    wait = (15 * 60) - seconds_into_window + 5  # +5s buffer past window boundary
    print(f"\nRate limit hit — sleeping {wait}s until next 15-min window...", flush=True)
    time.sleep(wait)


def _run(conn: sqlite3.Connection, full: bool) -> None:
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

    # Lazy lap sync: fetch laps for workout activities that have none yet.
    unsynced = conn.execute("""
        SELECT activity_id, name FROM activities
        WHERE run_type = 'workout'
          AND activity_id NOT IN (SELECT DISTINCT activity_id FROM laps)
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


@click.command()
@click.option("--full", is_flag=True, help="Ignore last sync date and fetch everything.")
def main(full: bool) -> None:
    conn = db.connect()
    db.init_db(conn)
    while True:
        try:
            _run(conn, full)
            break
        except Fault as e:
            if e.response is not None and e.response.status_code == 429:
                _wait_for_rate_limit()
            else:
                raise


if __name__ == "__main__":
    main()
