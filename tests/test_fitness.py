import sqlite3
from datetime import date

from helpers import activity_row

from miles import db
from miles.fitness import estimate_fitness


def test_hilly_race_stays_out_of_the_road_pace_projection(conn: sqlite3.Connection) -> None:
    road = activity_row(1, "2026-05-02T07:30:00", workout_type=1, name="Road marathon", distance_m=42200.0,
                        moving_time_s=10800, average_speed_mps=42200.0 / 10800, total_elevation_gain_m=150.0)
    trail = activity_row(2, "2026-09-19T07:00:00", workout_type=1, name="Trail 50K", distance_m=50000.0,
                         moving_time_s=21600, average_speed_mps=50000.0 / 21600, total_elevation_gain_m=1500.0)
    db.upsert_activities(conn, [road, trail])

    est = estimate_fitness(conn, date(2026, 9, 26))

    assert est is not None
    assert [s["activity_id"] for s in est["sources"]] == [1]
    assert est["confidence"] == "medium", "the road race is the newest signal, and it is over 90 days old"
