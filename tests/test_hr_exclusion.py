from datetime import date

import pytest
from fastapi.testclient import TestClient
from helpers import activity_row, insert_activity, memory_db
from test_easy_efficiency import STEADY_RUN, _segments

from miles import api, db, fitness, load
from miles import easy_efficiency as ee


def _flag(conn, activity_id: int, excluded: bool = True) -> None:
    db.set_hr_excluded(conn, activity_id, excluded)
    conn.commit()


def test_a_resync_keeps_the_flag():
    conn = memory_db()
    db.upsert_activities(conn, [activity_row(1, average_heartrate=150.0)])
    _flag(conn, 1)
    db.upsert_activities(conn, [activity_row(1, average_heartrate=190.0)])
    assert conn.execute("SELECT hr_excluded_at FROM activities").fetchone()[0] is not None


def test_unflagging_clears_it():
    conn = memory_db()
    insert_activity(conn, 1)
    _flag(conn, 1)
    _flag(conn, 1, False)
    assert conn.execute("SELECT hr_excluded_at FROM activities").fetchone()[0] is None


def test_a_flagged_run_leaves_the_easy_efficiency_chart():
    conn = memory_db()
    for aid in (1, 2):
        insert_activity(conn, aid, start_date=f"2026-10-0{aid}T07:00:00")
        _segments(conn, aid, STEADY_RUN)
    _flag(conn, 2)
    assert [r["activity_id"] for r in ee.easy_efficiency(conn)] == [1]


def test_a_flagged_run_is_charged_the_median_load_rate():
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00", moving_time_s=3600, average_heartrate=140.0)
    insert_activity(conn, 2, start_date="2026-10-02T07:00:00", moving_time_s=1800, average_heartrate=195.0)
    _flag(conn, 2)
    loads = load.daily_loads(conn, date(2026, 10, 1), date(2026, 10, 2), max_hr=200)
    assert loads["estimated_runs"] == 1
    # Half the time of the only scored run, at its rate.
    assert loads["run"]["2026-10-02"] == pytest.approx(loads["run"]["2026-10-01"] / 2)


def test_a_flagged_spike_does_not_raise_the_hr_ceiling():
    conn = memory_db()
    for aid in range(1, 12):
        insert_activity(conn, aid, start_date=f"2026-09-{aid:02d}T07:00:00", max_heartrate=180.0)
    insert_activity(conn, 99, start_date="2026-09-20T07:00:00", max_heartrate=240.0)
    _flag(conn, 99)
    assert fitness.hr_ceiling(conn, date(2026, 10, 1)) == 180.0


def test_the_activity_page_sets_and_reports_the_flag(monkeypatch):
    conn = memory_db()
    insert_activity(conn, 1, average_heartrate=150.0, strava_url="https://www.strava.com/activities/1", description="")
    monkeypatch.setattr(api, "_conn", lambda: conn)
    client = TestClient(api.app)
    res = client.post("/api/activity-reclassify?id=1", json={"run_type": "easy", "hr_excluded": True})
    assert res.status_code == 200 and res.json()["hr_excluded"] is True
    detail = client.get("/api/activity-detail?id=1").json()
    assert detail["hr_excluded"] is True
    assert detail["avg_hr"] == 150  # still shown on its own page


def test_flagging_alone_leaves_the_classification_alone(monkeypatch):
    conn = memory_db()
    insert_activity(conn, 1, sport_type="RockClimbing", run_type="easy", workout_type=0, strava_url="", description="")
    monkeypatch.setattr(api, "_conn", lambda: conn)
    res = TestClient(api.app).post("/api/activity-reclassify?id=1", json={"hr_excluded": True})
    assert res.status_code == 200
    row = conn.execute("SELECT workout_type, hr_excluded_at FROM activities").fetchone()
    assert row["workout_type"] == 0  # not pinned to athlete-confirmed easy (100)
    assert row["hr_excluded_at"] is not None
