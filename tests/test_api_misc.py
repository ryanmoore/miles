import pytest
from fastapi.testclient import TestClient

from miles import api


def _clear_tile_env(monkeypatch):
    for name in ("MILES_TILE_URL", "MILES_TILE_ATTRIBUTION", "MILES_TILE_MAX_ZOOM"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "env, expected",
    [
        (
            {"MILES_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png", "MILES_TILE_ATTRIBUTION": "Tiles by Example", "MILES_TILE_MAX_ZOOM": "16"},
            {"tile_url": "https://tiles.example/{z}/{x}/{y}.png", "tile_attribution": "Tiles by Example", "tile_max_zoom": 16},
        ),
        # A custom layer without a credit is credited to OpenStreetMap, whose data any tile server built on it carries.
        ({"MILES_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png"}, {"tile_url": "https://tiles.example/{z}/{x}/{y}.png", "tile_attribution": api._DEFAULT_TILE_ATTRIBUTION, "tile_max_zoom": None}),
        # An empty URL or a non-numeric zoom counts as unset.
        ({"MILES_TILE_URL": "", "MILES_TILE_MAX_ZOOM": "deep"}, {"tile_url": None, "tile_attribution": None, "tile_max_zoom": None}),
    ],
    ids=["configured layer", "no credit given", "empty or invalid"],
)
def test_config_serves_the_configured_tile_layer(monkeypatch, env: dict, expected: dict):
    _clear_tile_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert TestClient(api.app).get("/api/config").json() == expected


def _capture_sync_history(monkeypatch) -> list[bool]:
    seen: list[bool] = []

    def fake_run_sync(conn, *, full, history, progress):
        seen.append(history)
        return 0

    monkeypatch.setattr(api.sync, "run_sync", fake_run_sync)
    monkeypatch.setattr(api.sync, "PROGRESS", api.sync.SyncProgress())
    return seen


def test_ui_sync_skips_the_history_pass_even_when_history_is_enabled(monkeypatch):
    monkeypatch.setenv("MILES_SYNC_HISTORY", "1")
    seen = _capture_sync_history(monkeypatch)
    api.sync._run_in_background(False)
    assert seen == [False]


def test_sync_endpoint_runs_history_only_when_asked(monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(api.sync, "start_background", lambda history: started.append(history) or True)
    client = TestClient(api.app)

    assert client.post("/api/sync").json() == {"status": "started"}
    assert client.post("/api/sync?history=true").json() == {"status": "started"}
    assert started == [False, True]


def test_activities_list_includes_non_runs_without_run_fields(monkeypatch):
    from helpers import insert_activity, memory_db

    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-20T08:00:00", distance_m=5000, moving_time_s=1500, average_speed_mps=3.33)
    insert_activity(conn, 2, start_date="2026-10-19T18:00:00", sport_type="RockClimbing", distance_m=0, moving_time_s=7200, average_heartrate=105)
    monkeypatch.setattr(api, "_conn", lambda: conn)

    rows = TestClient(api.app).get("/api/activities-list").json()["rows"]

    assert [(r["id"], r["is_run"], r["sport_type"]) for r in rows] == [(1, True, "Run"), (2, False, "RockClimbing")]
    assert rows[0]["run_type"] == "easy" and rows[0]["pace_min_per_mile"] is not None
    assert (rows[1]["run_type"], rows[1]["distance_mi"], rows[1]["pace_min_per_mile"], rows[1]["avg_hr"]) == (None, None, None, 105)


def _filter_db(monkeypatch):
    from helpers import insert_activity, memory_db

    conn = memory_db()
    insert_activity(conn, 1, start_date="2026-10-20T08:00:00", run_type="easy")
    insert_activity(conn, 2, start_date="2026-09-15T08:00:00", run_type="workout", workout_type=3)
    insert_activity(conn, 3, start_date="2026-09-14T18:00:00", sport_type="RockClimbing")
    insert_activity(conn, 4, start_date="2026-07-31T08:00:00", sport_type="TrailRun", run_type="easy")
    monkeypatch.setattr(api, "_conn", lambda: conn)
    return TestClient(api.app)


@pytest.mark.parametrize(
    "query, expected_ids",
    [
        ("", [1, 2, 3, 4]),
        ("?sport=RockClimbing", [3]),
        ("?sport=Run&run_type=workout", [2]),
        # run_type narrows Run rows only: the trail run and the climb stay.
        ("?sport=Run&sport=TrailRun&sport=RockClimbing&run_type=workout", [2, 3, 4]),
        ("?from_month=2026-08&to_month=2026-09", [2, 3]),
        ("?from_month=2026-09", [1, 2, 3]),
        ("?to_month=2026-07", [4]),
    ],
)
def test_activities_list_filters(monkeypatch, query: str, expected_ids: list[int]):
    client = _filter_db(monkeypatch)
    assert [r["id"] for r in client.get(f"/api/activities-list{query}").json()["rows"]] == expected_ids


def test_activities_list_rejects_a_malformed_month(monkeypatch):
    assert _filter_db(monkeypatch).get("/api/activities-list?from_month=2026-13").status_code == 422


def test_activity_filters_lists_what_exists(monkeypatch):
    body = _filter_db(monkeypatch).get("/api/activity-filters").json()
    assert body["sports"] == [
        {"sport_type": "Run", "count": 2}, {"sport_type": "RockClimbing", "count": 1}, {"sport_type": "TrailRun", "count": 1},
    ]
    assert sorted((t["run_type"], t["count"]) for t in body["run_types"]) == [("easy", 1), ("workout", 1)]
    assert (body["first_month"], body["last_month"]) == ("2026-07", "2026-10")
