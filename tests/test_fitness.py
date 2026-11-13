import sqlite3
from datetime import date

from helpers import activity_row

from miles import db
from miles.derive import derive_all
from miles.fitness import estimate_fitness


def test_trail_and_hilly_races_stay_out_of_the_road_pace_projection(conn: sqlite3.Connection) -> None:
    road = activity_row(1, "2026-05-02T07:30:00", workout_type=1, name="Road marathon", distance_m=42200.0,
                        moving_time_s=10800, average_speed_mps=42200.0 / 10800, total_elevation_gain_m=150.0)
    hilly = activity_row(2, "2026-09-19T07:00:00", workout_type=1, name="Hilly 50K", distance_m=50000.0,
                         moving_time_s=21600, average_speed_mps=50000.0 / 21600, total_elevation_gain_m=1500.0)
    trail = activity_row(3, "2026-09-12T07:00:00", workout_type=1, sport="TrailRun", name="Flat trail half",
                         distance_m=21100.0, moving_time_s=7200, average_speed_mps=21100.0 / 7200)
    db.upsert_activities(conn, [road, hilly, trail])

    est = estimate_fitness(conn, date(2026, 9, 26))

    assert est is not None
    assert [s["activity_id"] for s in est["sources"]] == [1]
    assert est["confidence"] == "medium", "the road race is the newest signal, and it is over 90 days old"


def test_a_race_is_judged_without_the_jogged_one_before_it(conn: sqlite3.Connection) -> None:
    def race(activity_id: int, start: str, meters: float, seconds: int, hr: float) -> db.ActivityRow:
        return activity_row(activity_id, start, workout_type=1, distance_m=meters, moving_time_s=seconds,
                            average_speed_mps=meters / seconds, average_heartrate=hr, max_heartrate=190.0)

    db.upsert_activities(conn, [
        race(1, "2025-03-01T07:30:00", 42200.0, 10800, 160.0),
        race(2, "2025-04-05T07:30:00", 21100.0, 7200, 130.0),  # jogged
        race(3, "2025-05-10T07:30:00", 21100.0, 6400, 130.0),  # jogged too, though faster than the last one
    ])
    derive_all(conn, full=True)

    efforts = dict(conn.execute("SELECT activity_id, race_effort FROM activities").fetchall())
    assert efforts[2] == efforts[3] == "casual"
    half = dict(conn.execute("SELECT month, pace_half FROM fitness_checkpoints").fetchall())
    assert half["2025-05"] == half["2025-03"]
