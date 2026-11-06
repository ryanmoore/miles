import sqlite3

import pytest
from fastapi.testclient import TestClient
from helpers import call_tool, insert_activity

from miles import api, db, mcp_server

END = "2026-10-18"  # a Sunday; the week of 2026-10-12 is the latest


def _tool(name: str, **args):
    return call_tool(mcp_server.mcp, name, args)


def _segments(conn: sqlite3.Connection, activity_id: int, start_date: str, parts: list[tuple[int, float, float]]) -> None:
    """Stored 20 m terrain segments from (count, speed m/s, grade %) parts."""
    rows, idx = [], 0
    for count, speed, grade in parts:
        for _ in range(count):
            rows.append((activity_id, idx, 20.0 * idx, round(grade * 10), round(speed * 100), 140 + idx // 100))
            idx += 1
    conn.execute("INSERT INTO terrain_segment_dates (activity_id, start_date, segment_count) VALUES (?, ?, ?)", [activity_id, start_date, idx])
    conn.executemany(
        "INSERT INTO terrain_segments (activity_id, idx, dist_m, grade_dpct, speed_cms, heartrate, is_walking, ele_source) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
        rows,
    )


def _laps(conn: sqlite3.Connection, activity_id: int, reps: int, rep_speed: float) -> None:
    """Warmup mile, `reps` 1000 m reps with 200 m jogs, cooldown mile."""
    shape = [(1609.0, 3.0)] + [(d, s) for _ in range(reps) for d, s in ((1000.0, rep_speed), (200.0, 2.5))] + [(1609.0, 3.0)]
    for i, (dist, speed) in enumerate(shape):
        conn.execute(
            "INSERT INTO laps (activity_id, lap_index, distance_m, moving_time_s, elapsed_time_s, average_speed_mps, average_heartrate, max_heartrate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [activity_id, i, dist, round(dist / speed), round(dist / speed), speed, 150 + 3 * (i % 2 == 1) * 5 + i, 170 + i],
        )


@pytest.fixture
def seeded() -> None:
    conn = db.connect()
    insert_activity(conn, 1, start_date="2026-10-01T08:00:00", name="LT intervals", workout_label="LT", workout_type=3, run_type="workout", distance_m=9000.0, moving_time_s=2900)
    insert_activity(conn, 2, start_date="2026-10-13T08:00:00", name="LT intervals", workout_label="LT", workout_type=3, run_type="workout", distance_m=9000.0, moving_time_s=2850)
    insert_activity(conn, 3, start_date="2026-10-11T08:00:00", name="Long", workout_type=2, run_type="long_run", distance_m=14500.0, moving_time_s=4500, total_elevation_gain_m=60.0)
    insert_activity(conn, 4, start_date="2026-10-15T08:00:00", name="Easy with strides", distance_m=6400.0, moving_time_s=2000)
    insert_activity(conn, 5, start_date="2026-10-16T08:00:00", name="Easy", distance_m=5000.0, moving_time_s=1600, description="  Felt flat\n\n  but fine ")
    conn.execute("INSERT INTO weather (activity_id, fetched_at, temp_c_avg, wind_kph_avg) VALUES (5, 'x', 10.0, 17.6)")
    insert_activity(conn, 6, start_date="2026-07-14T08:00:00", name="Old easy", distance_m=8000.0, moving_time_s=2500)
    insert_activity(conn, 7, start_date="2026-10-14T08:00:00", name="Ride", sport_type="Ride")
    for i, day in enumerate(["05", "07", "12", "14", "19", "21"]):
        insert_activity(conn, 20 + i, start_date=f"2026-01-{day}T08:00:00", distance_m=8000.0)
    _laps(conn, 1, 5, 4.0)
    _laps(conn, 2, 5, 4.1)
    _segments(conn, 3, "2026-10-11", [(450, 3.0, 5.0), (450, 3.2, -5.0)])
    _segments(conn, 4, "2026-10-15", [(250, 3.2, 0.0)] + [p for _ in range(5) for p in ((6, 5.8, 0.0), (24, 3.2, 0.0))] + [(20, 3.2, 0.0)])
    conn.execute(
        "INSERT INTO plans (plan_id, title, created_at) VALUES (1, 'P', '2026-09-01')"
    )
    conn.execute("INSERT INTO plan_versions (version_id, plan_id, version_n, created_at, committed_at, author) VALUES (1, 1, 1, '2026-09-01', '2026-09-01', 'a')")
    conn.execute(
        "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, target_workouts, phase) "
        "VALUES (1, '2026-10-12', 30, 35, 1, 'build'), (1, '2026-10-19', 30, NULL, 1, 'build'), (1, '2026-07-13', 25, NULL, 0, 'base')"
    )
    conn.execute(
        "INSERT INTO plan_days (version_id, date, seq, slot, title, target_miles, note) VALUES "
        "(1, '2026-10-17', 1, 'long', 'Long', 14, NULL), (1, '2026-10-20', 1, 'workout', 'Tempo', 8, 'Steady'), (1, '2026-10-26', 1, 'easy', 'Later', 5, NULL)"
    )
    conn.execute("INSERT INTO plan_log (plan_id, date, action, reason, created_at) VALUES (1, '2026-10-14', 'skipped', 'slept badly', 'x')")
    conn.commit()
    conn.close()


def test_a_run_note_is_partially_updated_normalized_and_validated(seeded):
    note = _tool(
        "set_run_note", activity_id=5,
        fields={"short": "Easy loop", "tags": ["Poor Sleep", "heat", "poor_sleep"], "feel": "legs heavy", "kind": "Easy Run"},
    )
    assert (note["tags"], note["kind"], note["athlete_confirmed"]) == (["poor_sleep", "heat"], "easy_run", False)

    _tool("set_run_note", activity_id=5, fields={"feel": None, "elements": [{"type": "strides", "count": 4, "source": "athlete"}]})
    (got,) = _tool("get_run_notes", activity_ids=[5])
    assert got["short"] == "Easy loop" and "feel" not in got and got["elements"][0]["count"] == 4

    for activity_id, fields, message in [
        (6, {"feel": "ok"}, "short is required"),
        (5, {"short": None}, "short is required"),
        (5, {"short": "x" * 161}, "at most 160"),
        (5, {"elements": [{"type": "jumps", "count": 2, "source": "athlete"}]}, "elements.type"),
        (5, {"plan_delta": {"status": "late"}}, "plan_delta.status"),
        (5, {"colour": "red"}, "unknown fields"),
        (7, {"short": "ride"}, "not a synced run"),
    ]:
        assert message in _tool("set_run_note", activity_id=activity_id, fields=fields)["error"]


def test_a_week_note_is_stored_under_its_monday_and_merged(seeded):
    _tool("set_week_note", week_start="2026-10-15", tags=["Travel", "down"])
    _tool("set_week_note", week_start="2026-10-12", note="Away for work")
    assert _tool("get_week_notes", start_date="2026-10-01") == [
        {"week_start": "2026-10-12", "tags": ["travel", "down"], "note": "Away for work", "updated_at": _tool("get_week_notes")[0]["updated_at"]}
    ]
    _tool("set_week_note", week_start="2026-10-12", tags=[])
    assert "tags" not in _tool("get_week_notes")[0]
    assert "valid ISO date" in _tool("set_week_note", week_start="soon")["error"]


def test_unnoted_runs_lists_missing_notes_and_workouts_without_a_long_note(seeded):
    ids = lambda: [r["activity_id"] for r in _tool("list_unnoted_runs", start_date="2026-10-01", end_date=END)]  # noqa: E731
    assert ids() == [1, 3, 2, 4, 5]
    detected = {r["activity_id"]: r.get("detected_elements") for r in _tool("list_unnoted_runs", start_date="2026-10-01", end_date=END)}
    assert detected[4][0]["type"] == "strides" and detected[4][0]["count"] == 5 and detected[5] is None
    assert [r["desc"] for r in _tool("list_unnoted_runs", start_date="2026-10-16", end_date="2026-10-16")] == ["Felt flat but fine"]

    _tool("set_run_note", activity_id=5, fields={"short": "Easy"})
    _tool("set_run_note", activity_id=2, fields={"short": "LT day"})
    assert ids() == [1, 3, 2, 4]  # the workout still lacks a long note
    _tool("set_run_note", activity_id=2, fields={"long": "Reps held steady."})
    assert ids() == [1, 3, 4]


def test_detect_run_elements_reports_proposals_and_missing_streams(seeded):
    assert _tool("detect_run_elements", activity_id=4)["elements"][0]["type"] == "strides"
    assert "no stored stream" in _tool("detect_run_elements", activity_id=5)["error"]


def test_recent_history_fades_detail_with_age(seeded):
    _tool("set_run_note", activity_id=2, fields={"short": "LT day", "long": "Reps held steady.", "feel": "smooth"})
    _tool("set_run_note", activity_id=6, fields={"short": "Old easy"})
    _tool("set_week_note", week_start="2026-10-12", tags=["travel"])
    history = _tool("get_recent_history", weeks=2, end_date=END)

    this_week, last_week = history["weeks"][1], history["weeks"][0]
    assert (this_week["wk"], this_week["plan"], this_week["tags"], this_week["adj"]) == ("2026-10-12", "30-35", ["travel"], ["10-14 skipped: slept badly"])
    assert [r["id"] for r in this_week["r"]] == [2, 4, 5]  # the ride is not a run
    workout = this_week["r"][0]
    assert workout["note"] == "Reps held steady." and workout["feel"] == "smooth"
    assert workout["struct"].startswith("5×4:04 @ 6:33 avg")
    assert workout["vs"]["id"] == 1 and workout["vs"]["pace_delta_s"] < 0
    assert this_week["r"][1]["el_detected"][0]["count"] == 5
    easy = this_week["r"][2]
    assert (easy["desc"], easy["temp_f"], easy["wind_kph"]) == ("Felt flat but fine", 50, 18)
    assert [(d["d"], d["slot"]) for d in history["plan_days"]] == [("2026-10-17", "long"), ("2026-10-20", "workout")]
    assert history["last_activity"] == "2026-10-16"

    long_run = last_week["r"][0]
    assert long_run["splits"][0].startswith("1: ") and len(long_run["splits"]) == 11
    assert long_run["climb"] == long_run["descent"] == {"ft": 1476, "pct": 5.0}

    lines = {w["wk"]: w for w in history["earlier_weeks"]}
    assert lines["2026-09-28"]["sess"][0].startswith("5×4:10") and lines["2026-09-28"]["mi"] == 5.6
    assert lines["2026-07-13"]["plan"] == "25"
    assert [(p["start"], p["wks"]) for p in history["periods"]] == [("2026-01-05", 3)]
    assert len(history["earlier_weeks"]) == 25  # 26 weeks back, before the two detailed ones


def _lt_session(conn: sqlite3.Connection, activity_id: int, day: str, reps: int) -> None:
    insert_activity(conn, activity_id, start_date=f"{day}T08:00:00", workout_label="LT", workout_type=3, run_type="workout", distance_m=9000.0, moving_time_s=2900)
    _laps(conn, activity_id, reps, 4.0)


def test_a_session_is_compared_to_one_with_the_same_structure_before_the_latest_of_its_label():
    conn = db.connect()
    _lt_session(conn, 1, "2026-09-01", 5)
    _lt_session(conn, 2, "2026-09-20", 10)
    _lt_session(conn, 3, "2026-10-12", 5)
    _lt_session(conn, 4, "2026-10-14", 20)
    conn.commit()
    conn.close()
    runs = {r["id"]: r for w in _tool("get_recent_history", weeks=1, end_date=END)["weeks"] for r in w["r"]}
    assert (runs[3]["vs"]["id"], runs[3]["vs"]["match"]) == (1, "structure")
    assert (runs[4]["vs"]["id"], runs[4]["vs"]["match"]) == (3, "label")


def test_climbs_are_single_sustained_efforts_and_long_easy_runs_get_splits(seeded):
    conn = db.connect()
    insert_activity(conn, 30, start_date="2026-10-17T08:00:00", name="Long easy", distance_m=12000.0, moving_time_s=7300)
    insert_activity(conn, 31, start_date="2026-10-17T18:00:00", name="Short easy", distance_m=12000.0, moving_time_s=4000)
    flat_then_two_climbs = [(300, 3.0, 1.0), (100, 3.0, 7.0), (100, 3.0, -6.0), (60, 3.0, 7.0)]
    _segments(conn, 30, "2026-10-17", flat_then_two_climbs)
    _segments(conn, 31, "2026-10-17", flat_then_two_climbs)
    conn.commit()
    conn.close()
    runs = {r["id"]: r for r in _tool("get_recent_history", weeks=1, end_date=END)["weeks"][0]["r"]}
    assert runs[30]["climb"] == {"ft": 459, "pct": 7.0}
    assert "splits" in runs[30] and "splits" not in runs[31]


def test_notes_are_served_read_only_to_the_pages(seeded):
    _tool("set_run_note", activity_id=5, fields={"short": "Easy"})
    _tool("set_week_note", week_start="2026-10-12", tags=["travel"], note="Away")
    client = TestClient(api.app)
    assert client.get("/api/run-note?id=5").json()["short"] == "Easy"
    (week,) = client.get("/api/week-notes?start_date=2026-10-14&end_date=2026-10-18").json()
    assert (week["week_start"], week["tags"]) == ("2026-10-12", ["travel"])
