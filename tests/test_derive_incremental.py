"""derive_all's incremental path (scoped by the dirty-since date) must produce
the same derived values as a full rebuild, over a history with races, tagged
and untagged runs, typed laps, and an active plan."""

import json
import sqlite3
from datetime import date, timedelta

import pytest
from helpers import activity_row

from miles import db, derive, plan
from miles.derive import derive_all
from miles.inference import apply_inference

_FIRST_DAY = date(2025, 9, 1)
_RUNS = 96


def _lap(lap_id: int, activity_id: int, index: int, meters: float, seconds: int, hr: float) -> db.LapRow:
    return {
        "lap_id": lap_id, "activity_id": activity_id, "lap_index": index, "distance_m": meters,
        "moving_time_s": seconds, "elapsed_time_s": seconds, "average_speed_mps": meters / seconds,
        "average_heartrate": hr, "max_heartrate": hr + 8, "average_cadence": 85.0,
        "total_elevation_gain_m": 2.0, "pace_zone": None, "raw_json": "{}",
    }


def _history() -> tuple[list[db.ActivityRow], list[db.LapRow]]:
    """A run every three days on a twelve-run cycle: a tagged workout with
    repeats, a tagged long run, a tagged race, a named untagged race, long
    runs identified by name and by length, a slow untagged 10K, and easy runs
    of varying pace."""
    activities: list[db.ActivityRow] = []
    laps: list[db.LapRow] = []
    for i in range(_RUNS):
        activity_id = 1000 + i
        start = f"{(_FIRST_DAY + timedelta(days=3 * i)).isoformat()}T07:30:00"
        kind = i % 12
        if kind == 0:
            row = activity_row(activity_id, start, workout_type=3, name="Track 5 x 1000m", distance_m=9000.0, moving_time_s=2700, average_speed_mps=3.33)
            pieces = [(1600.0, 540, 140.0)] + [(1000.0, 225, 172.0), (400.0, 150, 140.0)] * 5 + [(1600.0, 560, 138.0)]
            laps += [_lap(activity_id * 100 + n, activity_id, n, m, s, hr) for n, (m, s, hr) in enumerate(pieces)]
        elif kind == 4:
            row = activity_row(activity_id, start, workout_type=2, distance_m=26000.0, moving_time_s=8700, average_speed_mps=2.99)
        elif kind == 5:
            row = activity_row(activity_id, start, name="Sunday long run", distance_m=21000.0, moving_time_s=6600, average_speed_mps=3.18)
        elif kind == 7:
            row = activity_row(activity_id, start, name="Hometown 5K race", distance_m=5000.0, moving_time_s=1200 + i, average_speed_mps=5000.0 / (1200 + i), average_heartrate=182.0)
        elif kind == 3:
            row = activity_row(activity_id, start, distance_m=24000.0, moving_time_s=7600, average_speed_mps=24000.0 / 7600, average_heartrate=146.0)
        elif kind == 9:
            row = activity_row(activity_id, start, name="Morning Run", distance_m=10000.0, moving_time_s=2300 - 3 * i, average_speed_mps=10000.0 / (2300 - 3 * i), average_heartrate=178.0)
        elif kind == 11:
            row = activity_row(activity_id, start, workout_type=1, name="Local 10K", distance_m=10000.0, moving_time_s=2400 + 6 * i, average_speed_mps=10000.0 / (2400 + 6 * i), average_heartrate=176.0)
            laps += [_lap(activity_id * 100 + n, activity_id, n, 1000.0, 240 + i // 4, 176.0) for n in range(10)]
        else:
            seconds = 2500 + 25 * (i % 7)
            row = activity_row(activity_id, start, distance_m=8000.0, moving_time_s=seconds, elapsed_time_s=seconds, average_speed_mps=8000.0 / seconds, average_heartrate=135.0 + i % 9)
        row["raw_json"] = json.dumps({"id": activity_id})
        activities.append(row)
    return activities, laps


def _add_plan(conn: sqlite3.Connection) -> None:
    plan_id, version_id = plan.start_plan_draft(conn, title="Spring 10K", race_date="2026-05-30", distance_bucket="10K")
    monday = date(2026, 3, 16)
    plan.upsert_draft_weeks(conn, version_id, [
        {"week_start": (monday + timedelta(weeks=n)).isoformat(), "target_workouts": 1, "phase": "base", "target_miles": 20.0 + n}
        for n in range(11)
    ])
    plan.commit_plan(conn, plan_id, note="initial")


def _snapshot(conn: sqlite3.Connection) -> tuple[object, ...]:
    queries = [
        "SELECT activity_id, run_type_inferred, race_effort, effort_ratio, workout_label, dominant_intensity "
        "FROM activities ORDER BY activity_id",
        "SELECT lap_id, lap_type, intensity FROM laps ORDER BY lap_id",
        "SELECT month, confidence, source_tier, pace_5k, pace_10k, pace_half, pace_marathon "
        "FROM fitness_checkpoints ORDER BY month",
        "SELECT plan_id, week_start, version_n_used, actual_miles, actual_workouts, "
        "long_run_done, mileage_ratio, workout_pace_delta_s, band, flags_json FROM plan_adherence ORDER BY plan_id, week_start",
    ]
    return tuple(tuple(tuple(r) for r in conn.execute(q).fetchall()) for q in queries)


def test_incremental_derive_matches_full_rebuild(conn: sqlite3.Connection) -> None:
    activities, laps = _history()
    db.upsert_activities(conn, activities)
    db.upsert_laps(conn, laps)
    _add_plan(conn)
    derive_all(conn, full=True)
    snapshot_full = _snapshot(conn)
    assert any(row[1] is not None for row in snapshot_full[1]), "the history should produce typed laps"
    assert snapshot_full[2], "the history should produce fitness checkpoints"
    assert snapshot_full[3], "the plan should produce adherence weeks"
    assert conn.execute(
        "SELECT COUNT(*) FROM activities WHERE name = 'Morning Run' AND run_type_inferred = 'race'"
    ).fetchone()[0] > 0, "the fast untagged 10Ks should be inferred as races from pace"

    newest = [a for a in activities if a["workout_type"] == 0][-4:] + [activities[-1]]
    db.upsert_activities(conn, newest)
    db.upsert_laps(conn, [lap for lap in laps if lap["activity_id"] in {a["activity_id"] for a in newest}])
    assert db.get_meta(conn, "derive_dirty_since") is not None

    derive_all(conn)

    assert db.get_meta(conn, "derive_dirty_since") is None
    assert _snapshot(conn) == snapshot_full

    derive_all(conn)
    assert _snapshot(conn) == snapshot_full


def _freeze_today(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    class Frozen(date):
        @classmethod
        def today(cls) -> "Frozen":
            return cls(day.year, day.month, day.day)

    monkeypatch.setattr(derive, "date", Frozen)


def test_a_month_without_a_derive_and_the_month_of_the_last_one_are_rebuilt(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    activities, laps = _history()
    db.upsert_activities(conn, activities)
    db.upsert_laps(conn, laps)
    _freeze_today(monkeypatch, date(2026, 6, 20))
    derive_all(conn, full=True)

    _freeze_today(monkeypatch, date(2026, 8, 5))
    derive_all(conn)
    after_rollover = _snapshot(conn)
    db.upsert_activities(conn, [activity_row(2000, "2026-08-04T07:30:00")])
    derive_all(conn)
    after_new_run = _snapshot(conn)

    derive_all(conn, full=True)
    assert after_new_run == _snapshot(conn)
    assert [row[0] for row in after_rollover[2]][-3:] == ["2026-06", "2026-07", "2026-08"]


def test_an_activity_that_loses_its_inferred_race_status_loses_its_effort(conn: sqlite3.Connection) -> None:
    db.upsert_activities(conn, [activity_row(1, "2026-01-05T08:00:00")])
    conn.execute("UPDATE activities SET race_effort = 'raced', effort_ratio = 0.9 WHERE activity_id = 1")

    apply_inference(conn)

    assert conn.execute("SELECT race_effort, effort_ratio FROM activities").fetchone()[:] == (None, None)
