from helpers import memory_db

from miles import gpx_parse, routes as routes_service


def _kml(body: str, ns: str = "http://www.opengis.net/kml/2.2") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="{ns}" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document><name>Doc</name>
{body}
</Document></kml>""".encode("utf-8")


def _coords(n: int, alt: float | None, lat0: float = 40.0) -> str:
    """n "lon,lat[,alt]" tuples heading north; a nonzero alt climbs 1 m per point."""
    out = []
    for i in range(n):
        tup = f"-105.0,{lat0 + i * 0.0002:.6f}"
        if alt is not None:
            tup += f",{alt + i if alt else 0}"
        out.append(tup)
    return " ".join(out)


def test_kml_linestrings_points_and_folders():
    body = f"""
<Placemark><name>Trailhead</name><description>Lot</description><Point><coordinates>-105.0,40.0,0</coordinates></Point></Placemark>
<Folder><name>F</name>
  <Placemark><name>Loop</name><LineString><coordinates>{_coords(10, 1600.0)}</coordinates></LineString></Placemark>
  <Placemark><name>Bad</name><LineString><coordinates>-105.0,40.0</coordinates></LineString></Placemark>
  <Placemark><name>Area</name><Polygon><outerBoundaryIs><LinearRing><coordinates>{_coords(5, None)}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Folder>
<Placemark><name>Multi</name><MultiGeometry>
  <LineString><coordinates>{_coords(3, 0)}</coordinates></LineString>
  <LineString><coordinates>{_coords(3, 0, lat0=40.01)}</coordinates></LineString>
</MultiGeometry></Placemark>
"""
    doc = gpx_parse.parse_gpx_bytes(_kml(body))
    assert [t["gpx_name"] for t in doc["tracks"]] == ["Loop", "Multi"]
    assert [t["track_index"] for t in doc["tracks"]] == [0, 1]
    loop, multi = doc["tracks"]
    assert loop["kind"] == "trk"
    assert loop["points"][0] == (40.0, -105.0, 1600.0)
    assert gpx_parse.has_full_elevation(loop["points"])
    assert len(multi["points"]) == 6
    # Clamped-to-ground paths write altitude 0 everywhere: no elevation.
    assert all(p[2] is None for p in multi["points"])
    assert doc["waypoints"] == [{"name": "Trailhead", "desc": "Lot", "lat": 40.0, "lng": -105.0}]


def test_kml_gx_track_and_old_namespace():
    coords = "".join(f"<gx:coord>-105.0 {40.0 + i * 0.0002:.6f} {1500 + i}</gx:coord>" for i in range(5))
    body = f"<Placemark><name>Rec</name><gx:Track>{coords}</gx:Track></Placemark>"
    doc = gpx_parse.parse_gpx_bytes(_kml(body, ns="http://earth.google.com/kml/2.2"))
    assert len(doc["tracks"]) == 1
    assert doc["tracks"][0]["points"][-1] == (40.0008, -105.0, 1504.0)


def test_kml_without_lines_is_rejected():
    body = "<Placemark><Point><coordinates>-105.0,40.0</coordinates></Point></Placemark>"
    try:
        gpx_parse.parse_gpx_bytes(_kml(body))
    except gpx_parse.GpxParseError:
        return
    raise AssertionError("expected GpxParseError")


def test_kml_upload_stores_raw_and_serves_gpx():
    body = f"""
<Placemark><name>Start</name><Point><coordinates>-105.0,40.0</coordinates></Point></Placemark>
<Placemark><name>Ridge</name><LineString><coordinates>{_coords(40, 1600.0)}</coordinates></LineString></Placemark>
"""
    data = _kml(body)
    conn = memory_db()
    route_ids = routes_service.upload_gpx(conn, data, "ridge.kml")
    assert len(route_ids) == 1
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["name"] == "Ridge"
    assert detail["has_elevation"] is True
    assert detail["distance_m"] is not None and detail["distance_m"] > 0
    stored = conn.execute("SELECT data FROM gpx_files").fetchone()["data"]
    assert bytes(stored) == data

    filename, gpx = routes_service.get_route_gpx(conn, route_ids[0])
    assert filename == "ridge.gpx"
    reparsed = gpx_parse.parse_gpx_bytes(gpx)
    assert not gpx_parse.is_kml(gpx)
    assert reparsed["tracks"][0]["gpx_name"] == "Ridge"
    assert len(reparsed["tracks"][0]["points"]) == 40
    assert reparsed["waypoints"][0]["name"] == "Start"


def test_http_upload_kml_with_the_web_form_fields_then_download_gpx():
    from fastapi.testclient import TestClient

    from miles import api

    body = f"<Placemark><name>Ridge</name><LineString><coordinates>{_coords(40, 1600.0)}</coordinates></LineString></Placemark>"
    client = TestClient(api.app)
    # routes.html sends every field, blank when unfilled.
    res = client.post(
        "/api/routes/upload",
        files={"file": ("ridge.kml", _kml(body), "application/vnd.google-earth.kml+xml")},
        data={"name": "", "source": "caltopo", "tags": "trail, hilly", "notes": ""},
    )
    assert res.status_code == 200, res.text
    [route] = res.json()
    assert route["name"] == "Ridge"
    assert route["source"] == "caltopo"
    assert route["tags"] == ["trail", "hilly"]

    gpx = client.get(f"/api/routes/{route['route_id']}/gpx")
    assert gpx.status_code == 200
    assert 'filename="ridge.gpx"' in gpx.headers["content-disposition"]
    assert gpx_parse.parse_gpx_bytes(gpx.content)["tracks"][0]["gpx_name"] == "Ridge"

    bad = client.post("/api/routes/upload", files={"file": ("bad.kml", b"<kml><Document/></kml>", "text/xml")})
    assert bad.status_code == 400
    assert "KML" in bad.json()["detail"]


def test_kml_path_with_real_elevation_then_zeros_has_no_elevation():
    """A path stitched from a recorded stretch and a drawn one (My Maps
    writes 0 on every drawn point) is dropped whole, not kept with a profile
    that falls to 0 partway along."""
    real = _coords(20, 30.0)
    drawn = _coords(10, 0, lat0=40.004)
    body = f"<Placemark><name>Stitched</name><LineString><coordinates>{real} {drawn}</coordinates></LineString></Placemark>"
    [track] = gpx_parse.parse_gpx_bytes(_kml(body))["tracks"]
    assert all(p[2] is None for p in track["points"])

    # A stray 0 or two (a point at the shore) keeps the file's elevation.
    few = _coords(30, 5.0) + " -105.0,40.01,0"
    body = f"<Placemark><name>Shore</name><LineString><coordinates>{few}</coordinates></LineString></Placemark>"
    [track] = gpx_parse.parse_gpx_bytes(_kml(body))["tracks"]
    assert gpx_parse.has_full_elevation(track["points"])
