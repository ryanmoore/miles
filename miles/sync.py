import click

from . import db, strava_client
from .classifier import classify_workout


@click.command()
@click.option("--full", is_flag=True, help="Ignore last sync date and fetch everything.")
def main(full: bool) -> None:
    conn = db.connect()
    db.init_db(conn)

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
            print(f"  {i}/{total_workouts} workouts ({lap_total} laps so far)...", end="\r")
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
        print(f"  {total_workouts}/{total_workouts} workouts — {lap_total} laps synced.    ")

    # Backfill labels for any workout activities that have laps but no label yet.
    unlabeled = conn.execute("""
        SELECT activity_id, name FROM activities
        WHERE run_type = 'workout' AND workout_label IS NULL
          AND activity_id IN (SELECT DISTINCT activity_id FROM laps)
    """).fetchall()
    for activity in unlabeled:
        name: str | None = activity["name"]
        if name:
            label = classify_workout(name)
            if label:
                conn.execute(
                    "UPDATE activities SET workout_label = ? WHERE activity_id = ?",
                    (label, activity["activity_id"]),
                )
    if unlabeled:
        conn.commit()


if __name__ == "__main__":
    main()
