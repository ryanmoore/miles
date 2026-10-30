import pytest
from helpers import insert_activity, memory_db

from miles import easy_efficiency as ee


def _segments(conn, activity_id: int, n: int, *, s_per_seg: int = 6, hr: int = 150, grade_dpct: int = 0,
              walking_from: int | None = None, pause_at: int | None = None) -> None:
    """n 20 m segments from time 0 at a steady pace; pause_at adds a 60 s
    stop before that segment."""
    rows = []
    t = 0
    for i in range(n):
        if i == pause_at:
            t += 60
        walking = int(walking_from is not None and i >= walking_from)
        rows.append((activity_id, i, i * 20.0, t, grade_dpct, hr, walking))
        t += s_per_seg
    conn.executemany(
        "INSERT INTO terrain_segments (activity_id, idx, dist_m, elapsed_s, grade_dpct, heartrate, is_walking, ele_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        rows,
    )
    conn.commit()


# 6 s per 20 m: 600 s of warmup is 100 segments, 20 min of counted time 200 more.
STEADY_RUN = 100 + 200 + 1


def test_ef_is_meters_per_beat_past_the_warmup():
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00")
    _segments(conn, 1, STEADY_RUN)
    [run] = ee.easy_efficiency(conn)
    assert run["minutes"] == 20.0
    assert run["avg_hr"] == 150.0
    # 20 m per 6 s at 150 bpm = 20 m per 15 beats.
    assert run["ef"] == pytest.approx(20 / 15, abs=1e-3)
    assert run["ef_gap"] == pytest.approx(run["ef"], abs=1e-3)
    assert run["heat_factor"] == 1.0


def test_uphill_running_earns_grade_adjusted_distance():
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00")
    _segments(conn, 1, STEADY_RUN, grade_dpct=50)
    [run] = ee.easy_efficiency(conn)
    assert run["ef_gap"] > run["ef"] * 1.1
    assert run["gap_pace_s"] < run["pace_s"]


def test_only_easy_runs_with_enough_steady_time_count():
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00", run_type="workout", workout_type=3)
    _segments(conn, 1, STEADY_RUN)
    insert_activity(conn, 2, start_date="2026-10-02T07:00:00")
    _segments(conn, 2, STEADY_RUN - 20)  # 2 min short
    insert_activity(conn, 3, start_date="2026-10-03T07:00:00")
    _segments(conn, 3, STEADY_RUN, walking_from=250)  # walking eats the counted time
    insert_activity(conn, 4, start_date="2026-10-04T07:00:00", sport_type="Ride")
    _segments(conn, 4, STEADY_RUN)
    assert ee.easy_efficiency(conn) == []


def test_a_pause_is_not_counted_as_running_time():
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00")
    _segments(conn, 1, STEADY_RUN + 10, pause_at=200)
    [run] = ee.easy_efficiency(conn)
    assert run["ef"] == pytest.approx(20 / 15, abs=1e-3)


def test_heat_factor_rides_along_for_the_client():
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-08-01T07:00:00")
    _segments(conn, 1, STEADY_RUN)
    conn.execute("INSERT INTO weather (activity_id, fetched_at, temp_c_avg, humidity_avg) VALUES (1, '2026-08-02', 30.0, 60.0)")
    conn.commit()
    [run] = ee.easy_efficiency(conn)
    assert run["heat_factor"] > 1.04
    assert run["ef"] == pytest.approx(20 / 15, abs=1e-3)  # not applied server-side
