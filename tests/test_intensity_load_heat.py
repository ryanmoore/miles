from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from helpers import insert_activity, memory_db

from miles import api, db, heat, intensity, load, load_api

TODAY = date(2026, 10, 21)  # a Wednesday


def _day(days_ago: int) -> str:
    return f"{TODAY - timedelta(days=days_ago):%Y-%m-%d}T08:00:00"


def _segments(conn, activity_id: int, hrs: list[int], speed_cms: int = 300) -> None:
    conn.executemany(
        "INSERT INTO terrain_segments (activity_id, idx, dist_m, elapsed_s, grade_dpct, speed_cms, heartrate, is_walking, ele_source) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, 0, 0)",
        [(activity_id, i, i * 20.0, i * 7, speed_cms, hr) for i, hr in enumerate(hrs)],
    )
    conn.commit()


@pytest.mark.parametrize(
    "temp_c, humidity, expected_pct",
    [
        (10.0, 50.0, 0.0),  # cool: temp + dew point well under 100 °F
        (30.0, 60.0, pytest.approx(5.5, abs=0.1)),  # 86 °F + ~70.6 °F dew point
        (40.0, 90.0, 10.0),  # past the table's end: clamped
    ],
)
def test_slowdown_follows_the_temp_plus_dew_point_table(temp_c, humidity, expected_pct):
    assert heat.slowdown_pct(temp_c, humidity) == expected_pct


def test_heat_factor_is_neutral_without_weather():
    assert heat.heat_factor(None, 50.0) == 1.0
    assert heat.heat_factor(30.0, None) == 1.0


def test_hr_intensity_splits_a_run_by_its_segments_and_scales_to_moving_time():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    insert_activity(conn, 1, start_date=_day(1), moving_time_s=3600, distance_m=10000, average_heartrate=160)
    _segments(conn, 1, [140] * 50 + [180] * 50)  # 70% and 90% of max, equal speed

    week = intensity.intensity_distribution(conn, 4, today=TODAY)["weeks"][-1]

    assert week["hr"] == {"easy": 1800, "moderate": 0, "hard": 1800, "unclassified": 0}


def test_hr_intensity_falls_back_to_average_hr_without_segments():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    insert_activity(conn, 1, start_date=_day(1), moving_time_s=1800, distance_m=5000, average_heartrate=168)  # 84%

    week = intensity.intensity_distribution(conn, 4, today=TODAY)["weeks"][-1]

    assert week["hr"]["moderate"] == 1800


def test_hr_intensity_without_max_hr_is_unclassified_with_a_note():
    conn = memory_db()
    insert_activity(conn, 1, start_date=_day(1), moving_time_s=1800, distance_m=5000, average_heartrate=150)

    result = intensity.intensity_distribution(conn, 4, today=TODAY)

    assert result["note"] is not None
    assert result["weeks"][-1]["hr"]["unclassified"] == 1800


def test_pace_intensity_uses_the_prior_months_checkpoint_and_heat_adjusts():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    conn.execute(
        "INSERT INTO fitness_checkpoints (month, confidence, source_tier, pace_5k, pace_10k, pace_half, pace_marathon) "
        "VALUES ('2026-09', 'high', 1, 7.0, 7.3, 7.7, 8.0)"
    )
    mile = 1609.34
    # 8:15/mi: just outside the MP band (8:00 +3%) in cool weather -> easy.
    insert_activity(conn, 1, start_date=_day(1), moving_time_s=495, distance_m=mile)
    # Same pace on a hot day: heat-adjusted to ~7:49, inside the MP band -> moderate.
    insert_activity(conn, 2, start_date=_day(2), moving_time_s=495, distance_m=mile)
    conn.execute(
        "INSERT INTO weather (activity_id, fetched_at, temp_c_avg, humidity_avg) VALUES (2, '2026-10-20', 30.0, 60.0)"
    )
    conn.commit()

    week = intensity.intensity_distribution(conn, 4, today=TODAY)["weeks"][-1]

    assert week["pace"] == {"easy": 495, "moderate": 495, "hard": 0, "unclassified": 0}


@pytest.mark.parametrize("pct, weight", [(49, 0), (50, 1), (69, 2), (75, 3), (85, 4), (90, 5), (105, 5)])
def test_edwards_weights_by_ten_percent_band(pct, weight):
    assert load.edwards_weight(pct) == weight


def test_steady_daily_load_gives_a_ratio_of_one():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    for i in range(70):
        insert_activity(conn, 100 + i, start_date=_day(i), moving_time_s=1800, distance_m=5000, average_heartrate=150)  # 75% -> x3

    current = load.load_series(conn, 4, today=TODAY)["current"]

    assert current is not None
    assert current["load"] == 90.0
    assert current["acwr"] == 1.0
    assert current["acwr_band"] == "optimal"
    assert current["monotony"] is None  # no day-to-day variation: sd 0


def test_a_spike_after_a_steady_block_reads_high():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    for i in range(7, 70, 2):
        insert_activity(conn, 100 + i, start_date=_day(i), moving_time_s=1800, distance_m=5000, average_heartrate=150)
    for i in range(7):
        insert_activity(conn, 200 + i, start_date=_day(i), moving_time_s=3600, distance_m=10000, average_heartrate=170)

    current = load.load_series(conn, 4, today=TODAY)["current"]

    assert current is not None and current["acwr"] is not None
    assert current["acwr"] > load.ACWR_CAUTION_MAX
    assert current["acwr_band"] == "high"


def test_ratio_is_null_until_28_days_of_history():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    for i in range(10):
        insert_activity(conn, 100 + i, start_date=_day(i), moving_time_s=1800, distance_m=5000, average_heartrate=150)

    days = load.load_series(conn, 4, today=TODAY)["days"]

    assert all(d["acwr"] is None for d in days)


def test_a_run_without_hr_is_charged_the_median_rate():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    insert_activity(conn, 1, start_date=_day(2), moving_time_s=1800, distance_m=5000, average_heartrate=150)  # 3/min
    insert_activity(conn, 2, start_date=_day(1), moving_time_s=600, distance_m=2000)

    result = load.load_series(conn, 4, today=TODAY)

    assert result["estimated_runs"] == 1
    assert result["days"][-2]["load"] == 30.0


def test_load_without_max_hr_reports_why():
    conn = memory_db()
    insert_activity(conn, 1, start_date=_day(1), moving_time_s=1800, distance_m=5000, average_heartrate=150)

    result = load.load_series(conn, 4, today=TODAY)

    assert result["days"] == [] and result["note"] is not None


def test_a_non_run_activity_adds_load_from_its_average_hr():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    insert_activity(conn, 1, start_date=_day(1), moving_time_s=1800, distance_m=5000, average_heartrate=150)  # 75% -> x3
    insert_activity(conn, 2, start_date=_day(1), sport_type="RockClimbing", moving_time_s=6000, average_heartrate=110)  # 55% -> x1

    day = load.load_series(conn, 4, today=TODAY)["days"][-2]

    assert (day["load_run"], day["load_other"], day["load"]) == (90.0, 100.0, 190.0)


def test_a_non_run_without_hr_adds_nothing_and_is_counted():
    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    insert_activity(conn, 1, start_date=_day(2), moving_time_s=1800, distance_m=5000, average_heartrate=150)
    insert_activity(conn, 2, start_date=_day(1), sport_type="WeightTraining", moving_time_s=1800)

    result = load.load_series(conn, 4, today=TODAY)

    assert result["unscored_other"] == 1 and result["estimated_runs"] == 0
    assert result["days"][-2]["load"] == 0.0


def test_endpoints_serve_a_multi_year_window_ending_in_the_past(monkeypatch):
    @contextmanager
    def fake_connection(_connect):
        yield conn

    conn = memory_db()
    db.upsert_athlete(conn, max_hr=200, long_run_floor_miles=None)
    insert_activity(conn, 1, start_date="2020-03-10T08:00:00", moving_time_s=1800, distance_m=5000, average_heartrate=150)
    monkeypatch.setattr(load_api.db, "connection", fake_connection)
    client = TestClient(api.app)

    days = client.get("/api/load?weeks=4&end=2020-03-15").json()["days"]
    assert (days[0]["date"], days[-1]["date"]) == ("2020-02-17", "2020-03-15")
    assert next(d["load"] for d in days if d["date"] == "2020-03-10") > 0

    weeks = client.get("/api/intensity-distribution?weeks=260&end=2020-03-15").json()["weeks"]
    assert (len(weeks), weeks[-1]["week_start"], weeks[-1]["hr"]["easy"]) == (260, "2020-03-09", 1800)
