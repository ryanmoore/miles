import json

from helpers import insert_activity, memory_db

from miles import mcp_server


def test_a_session_lists_short_reps_and_short_jogs(monkeypatch):
    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-01T07:00:00", name="Sets")
    # (meters, seconds): a 90 s rep, a jog under 200 m, a 30 s rep, a stopped lap.
    for index, (meters, seconds) in enumerate([(400.0, 90), (150.0, 60), (140.0, 30), (0.0, 20)]):
        conn.execute(
            "INSERT INTO laps (activity_id, lap_index, distance_m, moving_time_s, average_speed_mps)"
            " VALUES (1, ?, ?, ?, ?)",
            [index, meters, seconds, meters / seconds],
        )
    conn.commit()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    session = json.loads(mcp_server.get_workout_session(1))
    assert [lap["duration_s"] for lap in session["laps"]] == [90, 60, 30]
    assert session["laps_fetched"] is False
