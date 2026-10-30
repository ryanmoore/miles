import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from helpers import call_tool
from mcp.server.fastmcp import FastMCP

from miles import api, candidate_sets, db, route_search, routing_mcp

# A candidate climbing 6 m per 20 m step (a hike-grade slope) for its first
# half and dropping gently after: its classes differ along the line.
GEOMETRY = (
    [[40.0 + i * 0.00018, -105.0, 100.0 + i * 6.0] for i in range(20)]
    + [[40.0036 + i * 0.00018, -105.0, 214.0 - i * 0.4] for i in range(20)]
)


def _store(conn: sqlite3.Connection, candidate_id: str, *, hours: float = 24.0, drive_s: float | None = None, ranking: dict | None = None) -> None:
    start = {"lat": 40.0, "lng": -105.0, "label": "Lot"}
    if drive_s is not None:
        start["drive_s"] = drive_s
    data = {
        "area_id": 1, "shape": "out_and_back", "start": start, "distance_m": 8000.0,
        "score": {
            "gain_m": 120.0, "paved_distance_m": 500.0, "singletrack_distance_m": 5000.0, "track_distance_m": 1500.0,
            "runnability": {"run_pct": 60.0, "grind_pct": 25.0, "hike_pct": 15.0},
        },
        "missed_constraints": [], "geometry": GEOMETRY, "ways": [],
        "sections_used": [{"section_id": 3, "block_id": 3, "name": "Ridge Loop", "length_m": 4000.0}],
    }
    if ranking is not None:
        data["ranking"] = ranking
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    conn.execute("INSERT INTO net_candidates VALUES (?, ?, ?)", [candidate_id, expires, json.dumps(data)])
    conn.commit()


def test_a_set_lists_its_candidates_in_order_with_their_notes_and_summaries(conn: sqlite3.Connection):
    for cid in ("a", "b", "c"):
        _store(conn, cid, drive_s=600.0 if cid == "b" else None)

    set_id, _ = candidate_sets.create_set(conn, ["c", "a", "b"], "Options", {"a": "flattest"})
    view = candidate_sets.get_set(conn, set_id)

    assert view["title"] == "Options"
    assert [c["candidate_id"] for c in view["candidates"]] == ["c", "a", "b"]
    assert [c["note"] for c in view["candidates"]] == [None, "flattest", None]
    card = view["candidates"][2]
    assert card["start"] == {"label": "Lot", "lat": 40.0, "lng": -105.0, "drive_minutes": 10.0}
    assert (card["run_pct"], card["hike_pct"], card["singletrack_m"], card["paved_m"]) == (60.0, 15.0, 5000.0, 500.0)
    assert card["sections_used"][0]["name"] == "Ridge Loop"


def test_a_summary_passes_the_ranking_through_as_stored_and_is_null_without_one(conn: sqlite3.Connection):
    ranking = {"score": 1.5, "gain_fit": 0.9}
    _store(conn, "ranked", ranking=ranking)
    _store(conn, "plain")

    set_id, _ = candidate_sets.create_set(conn, ["ranked", "plain"], "T", None)
    ranked, plain = candidate_sets.get_set(conn, set_id)["candidates"]

    assert ranked["ranking"] == ranking
    assert plain["ranking"] is None


def test_sharing_keeps_candidates_for_the_whole_life_of_the_set(conn: sqlite3.Connection):
    _store(conn, "short", hours=1.0)
    _store(conn, "long", hours=24.0 * 30)

    _, expires_at = candidate_sets.create_set(conn, ["short", "long"], "T", None)

    expiries = dict(conn.execute("SELECT candidate_id, expires_at FROM net_candidates").fetchall())
    assert expiries["short"] == expires_at
    assert expiries["long"] > expires_at  # never shortened


def test_an_unknown_or_expired_candidate_cannot_be_shared_and_an_empty_set_is_refused(conn: sqlite3.Connection):
    _store(conn, "old", hours=-1.0)
    with pytest.raises(route_search.CandidateNotFoundError):
        candidate_sets.create_set(conn, ["old"], "T", None)
    with pytest.raises(ValueError):
        candidate_sets.create_set(conn, [], "T", None)


def test_an_expired_set_is_gone(conn: sqlite3.Connection):
    _store(conn, "a")
    set_id, _ = candidate_sets.create_set(conn, ["a"], "T", None)
    conn.execute("UPDATE candidate_sets SET expires_at = ?", [(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()])
    with pytest.raises(candidate_sets.CandidateSetNotFoundError):
        candidate_sets.get_set(conn, set_id)


@pytest.fixture
def share(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    server = FastMCP("test")
    routing_mcp.register_routing_tools(server)

    @contextmanager
    def lend(opener=None):
        yield conn

    monkeypatch.setattr(routing_mcp.db, "connection", lend)
    _store(conn, "a")
    return lambda: call_tool(server, "share_candidates", {"candidate_ids": ["a"], "title": "T"})


def test_share_candidates_returns_an_absolute_url_when_the_public_url_is_set_and_a_path_otherwise(share, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MILES_PUBLIC_URL", "https://miles.example.com/")
    result = share()
    assert result["url"] == f"https://miles.example.com/candidates.html?set={result['set_id']}"
    assert result["expires_at"]

    monkeypatch.delenv("MILES_PUBLIC_URL")
    assert share()["url"].startswith("/candidates.html?set=")


def test_http_set_candidate_points_and_save():
    with db.connection() as conn:
        _store(conn, "a")
        set_id, _ = candidate_sets.create_set(conn, ["a"], "T", {"a": "note"})
    client = TestClient(api.app)

    view = client.get(f"/api/candidate-sets/{set_id}").json()
    assert view["candidates"][0]["note"] == "note"

    points = client.get("/api/candidates/a").json()
    assert len(points) == len(GEOMETRY)
    assert points[0]["dist_m"] == 0.0 and points[0]["ele_m"] is not None
    assert points[10]["cls"] == "hike" and points[-5]["cls"] == "run"

    saved = client.post("/api/candidates/a/save").json()
    assert saved["name"] == "Out and back 5.0 mi"
    assert client.get(f"/api/routes/{saved['route_id']}").status_code == 200


def test_http_share_creates_a_set_that_reads_back_and_refuses_bad_ids():
    with db.connection() as conn:
        _store(conn, "a")
        _store(conn, "b")
    client = TestClient(api.app)

    shared = client.post("/api/candidate-sets", json={"candidate_ids": ["b", "a"], "title": "T", "notes": {"a": "n"}, "request": {"n": 5}})
    assert shared.status_code == 200
    view = client.get(f"/api/candidate-sets/{shared.json()['set_id']}").json()
    assert [c["candidate_id"] for c in view["candidates"]] == ["b", "a"]
    assert view["candidates"][1]["note"] == "n"
    assert view["request"] == {"n": 5}

    missing = client.post("/api/candidate-sets", json={"candidate_ids": ["a", "nope"], "title": "T"})
    assert missing.status_code == 404 and "nope" in missing.json()["detail"]
    assert client.post("/api/candidate-sets", json={"candidate_ids": [], "title": "T"}).status_code == 400


@pytest.mark.parametrize(
    "method, url",
    [("get", "/api/candidate-sets/nope"), ("get", "/api/candidates/nope"), ("post", "/api/candidates/nope/save")],
)
def test_unknown_ids_are_404_with_a_message(method: str, url: str):
    res = getattr(TestClient(api.app), method)(url)
    assert res.status_code == 404 and "nope" in res.json()["detail"]


def test_a_candidate_without_elevation_gets_no_classes(conn: sqlite3.Connection):
    from miles import terrain
    points = terrain.geometry_point_classes(conn, [[40.0, -105.0, None], [40.001, -105.0, None]])
    assert [p["cls"] for p in points] == [None, None]
