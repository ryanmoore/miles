import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN_LAT, ORIGIN_LNG, by_deg_lat, node, none_sampler, way

from miles import api, db, network, search_jobs

A_LAT = ORIGIN_LAT + by_deg_lat(1600.0)
B_LAT = A_LAT + by_deg_lat(200.0)
C_LNG = ORIGIN_LNG + by_deg_lat(200.0)

BODY = {
    "area_id": 1, "start_lat": A_LAT, "start_lng": ORIGIN_LNG,
    "distance_mi_min": 0.1, "distance_mi_max": 0.5, "shape": "loop", "seed": 3,
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A client over a database holding one ready area: a 600 m trail triangle."""
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO areas (label, center_lat, center_lng, radius_km, status, created_at, last_used_at) "
            "VALUES ('Test area', ?, ?, 2.0, 'ready', '2026-01-01', '2026-01-01')",
            [A_LAT, ORIGIN_LNG],
        )
        conn.execute(
            "INSERT INTO areas (label, center_lat, center_lng, radius_km, status, created_at, last_used_at) "
            "VALUES ('Half imported', ?, ?, 2.0, 'importing', '2026-01-01', '2026-01-01')",
            [A_LAT, ORIGIN_LNG],
        )
        conn.commit()
        payload = {"elements": [
            node(2, A_LAT, ORIGIN_LNG), node(3, B_LAT, ORIGIN_LNG), node(4, B_LAT, C_LNG),
            way(10, [2, 3], {"highway": "path"}), way(11, [3, 4], {"highway": "path"}), way(12, [4, 2], {"highway": "path"}),
        ]}
        network.build_network(conn, 1, [payload], none_sampler())
    monkeypatch.setattr(search_jobs, "_jobs", {})
    return TestClient(api.app)


def test_areas_lists_every_area_with_its_status_and_center(client: TestClient):
    areas = {a["label"]: a for a in client.get("/api/areas").json()}
    assert areas["Test area"]["status"] == "ready" and areas["Test area"]["center_lat"] == A_LAT
    assert areas["Half imported"]["status"] == "importing"


def test_a_search_starts_at_once_and_a_waiting_get_returns_its_candidate_cards(client: TestClient):
    started = client.post("/api/searches", json=BODY)
    assert started.status_code == 200 and started.json()["search_id"]

    done = client.get(f"/api/searches/{started.json()['search_id']}", params={"wait_s": 30}).json()
    assert done["status"] == "done" and done["candidates"]
    card = done["candidates"][0]
    assert card["shape"] == "loop" and card["missed_constraints"] == [] and card["start"]["lat"] == A_LAT
    assert client.get(f"/api/candidates/{card['candidate_id']}").json()


def test_a_search_can_be_cancelled(client: TestClient):
    search_id = client.post("/api/searches", json=BODY).json()["search_id"]
    assert client.post(f"/api/searches/{search_id}/cancel").json()["status"] in ("cancelled", "done")


def test_a_bad_request_is_a_400_with_the_reason_and_an_unknown_search_a_404(client: TestClient):
    bad = client.post("/api/searches", json={**BODY, "distance_mi_min": 9, "distance_mi_max": 2})
    assert bad.status_code == 400 and "distance_m_min" in bad.json()["detail"]
    assert client.post("/api/searches", json={**BODY, "area_id": 2}).status_code == 400
    assert client.get("/api/searches/nope").status_code == 404
    assert client.post("/api/searches/nope/cancel").status_code == 404


def test_a_candidate_listed_before_the_search_ends_keeps_its_id_in_the_final_list(client: TestClient):
    search_id = client.post("/api/searches", json=BODY).json()["search_id"]
    done = client.get(f"/api/searches/{search_id}", params={"wait_s": 30}).json()
    with db.connection() as conn:
        listed_midway = search_jobs._jobs[search_id].progress.snapshot(conn)
    assert done["candidates"]
    assert [c["candidate_id"] for c in listed_midway] == [c["candidate_id"] for c in done["candidates"]]
