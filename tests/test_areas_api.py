from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import ImmediateThread

from miles import api, areas, db, geocode

LAT, LNG = 5.0, 10.0


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


def _area(conn, label: str, region: str, status: str = "ready", lat: float = LAT) -> int:
    cur = conn.execute(
        "INSERT INTO areas (label, center_lat, center_lng, radius_km, status, region, created_at, last_used_at) "
        "VALUES (?, ?, ?, 2.0, ?, ?, '2026-01-01', '2026-01-01')",
        (label, lat, LNG, status, region),
    )
    conn.commit()
    return int(cur.lastrowid)


def _extract(conn, tmp_path: Path, region: str) -> Path:
    path = tmp_path / (region.replace("/", "-") + ".osm.pbf")
    path.write_bytes(b"x")
    conn.execute(
        "INSERT INTO map_extracts (region, path, sha256, fetched_at, checked_at, data_as_of) VALUES (?, ?, 'x', 'd', 'd', 'd')",
        (region, str(path)),
    )
    conn.commit()
    return path


def test_removing_an_area_drops_its_rows_and_only_the_extracts_no_other_area_needs(client: TestClient, tmp_path: Path):
    with db.connection() as conn:
        doomed = _area(conn, "Doomed", "r/only-here")
        _area(conn, "Kept", "r/shared")
        only_here = _extract(conn, tmp_path, "r/only-here")
        shared = _extract(conn, tmp_path, "r/shared")
        conn.execute("INSERT INTO area_tiles (area_id, gx, gy) VALUES (?, 1, 1)", (doomed,))
        conn.execute(
            "INSERT INTO map_tiles (gx, gy, query_version, fetched_at, sha256, data, source) VALUES (1, 1, 1, 'd', 'x', x'00', 's')"
        )
        conn.commit()

    res = client.delete(f"/api/areas/{doomed}")

    assert res.json() == {"freed_extracts": ["r/only-here"]}
    assert not only_here.exists() and shared.exists()
    with db.connection() as conn:
        assert [r["label"] for r in conn.execute("SELECT label FROM areas")] == ["Kept"]
        assert conn.execute("SELECT COUNT(*) FROM area_tiles").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM map_tiles").fetchone()[0] == 0
        assert [e["region"] for e in conn.execute("SELECT region FROM map_extracts")] == ["r/shared"]


def test_an_importing_area_cannot_be_removed_and_an_unknown_one_is_a_404(client: TestClient):
    with db.connection() as conn:
        busy = _area(conn, "Busy", "r/a", status="importing")

    assert client.delete(f"/api/areas/{busy}").status_code == 409
    assert client.delete("/api/areas/999").status_code == 404


def test_posting_an_area_starts_one_import_at_the_confirmed_point_without_geocoding(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    started: list[int] = []
    monkeypatch.setattr(areas.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(areas, "_run_import_job", lambda area_id, refresh=False: started.append(area_id))
    monkeypatch.setattr(geocode, "lookup", lambda *a, **k: pytest.fail("geocoded"))

    estimate = client.get("/api/areas/estimate", params={"lat": LAT, "lng": LNG, "radius_km": 12}).json()
    assert estimate["estimated_seconds"] is None and estimate["source"].startswith("pending-extract")

    res = client.post("/api/areas", json={"label": "Springfield", "lat": LAT, "lng": LNG, "radius_km": 12})

    assert res.status_code == 200 and res.json()["label"] == "Springfield"
    assert started == [res.json()["area_id"]]
    assert client.post("/api/areas", json={"label": "Far", "lat": LAT, "lng": LNG, "radius_km": 500}).status_code == 400


def test_pinning_toggles_an_area_and_an_unknown_one_is_a_404(client: TestClient):
    with db.connection() as conn:
        area_id = _area(conn, "Pinnable", "r/a")

    assert client.post(f"/api/areas/{area_id}/pin", json={"pinned": True}).json()["pinned"] is True
    assert [a["pinned"] for a in client.get("/api/areas").json()] == [True]
    assert client.post("/api/areas/999/pin", json={"pinned": True}).status_code == 404


def test_geocode_answers_one_lookup_and_maps_no_match_to_404(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    answers = iter([(1.5, 2.5, "123 Main St, Springfield"), geocode.NoMatchError("none")])

    def fetch(query: str):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(geocode, "_fetch_nominatim", fetch)

    found = client.get("/api/geocode", params={"q": "123 Main St"}).json()
    assert (found["lat"], found["lng"], found["display_name"]) == (1.5, 2.5, "123 Main St, Springfield")
    assert client.get("/api/geocode", params={"q": "Nowhere"}).status_code == 404
