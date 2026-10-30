from helpers import insert_activity, memory_db

from miles import api, db


def _lap(conn, activity_id: int, lap_index: int, hr: float, *, distance_m: float = 1609.34, moving_time_s: int = 480) -> None:
    conn.execute(
        "INSERT INTO laps (activity_id, lap_index, distance_m, moving_time_s, average_heartrate) VALUES (?, ?, ?, ?, ?)",
        [activity_id, lap_index, distance_m, moving_time_s, hr],
    )
    conn.commit()


def test_each_point_names_its_run_and_lap(monkeypatch):
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00", name="Tempo")
    _lap(conn, 1, 1, 150.4)
    _lap(conn, 1, 2, 152.0)
    monkeypatch.setattr(api, "_conn", lambda: conn)
    out = api.get_hr_pace_heatmap()
    assert [(p["activity_id"], p["lap_index"], p["hr"], p["pace_s"]) for p in out["points"]] == [
        (1, 1, 150.4, 480.0), (1, 2, 152.0, 480.0),
    ]
    assert out["points"][0]["hr_bucket"] == 150 and out["points"][0]["pace_bucket"] == 480
    assert out["activities"] == [{"activity_id": 1, "name": "Tempo", "date": "2026-10-01"}]


def test_a_flagged_runs_laps_are_left_out(monkeypatch):
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00")
    _lap(conn, 1, 1, 150.0)
    db.set_hr_excluded(conn, 1, True)
    conn.commit()
    monkeypatch.setattr(api, "_conn", lambda: conn)
    assert api.get_hr_pace_heatmap() == {"points": [], "activities": []}
