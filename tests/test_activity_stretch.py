import gzip
import json
import sqlite3

import pytest
from helpers import call_tool, climbing_sampler, none_sampler
from mcp.server.fastmcp import FastMCP
from test_sections import Map, pt
from test_survey import insert_area

from miles import db, elevation, streams, terrain_mcp

HILL_START_M, HILL_END_M, HILL_RISE_M = 200.0, 260.0, 12.0


@pytest.fixture
def conn():
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def server() -> FastMCP:
    server = FastMCP("t")
    terrain_mcp.register_terrain_tools(server)
    return server


def write_run(activity_id: int, *, gps: bool = True) -> None:
    """A 5 m-sample run east along the origin road, flat but for a 20% hill."""
    raw: dict = {"distance": {"data": [i * 5.0 for i in range(161)]}}
    raw["altitude"] = {"data": [
        100.0 + HILL_RISE_M * min(max((i * 5.0 - HILL_START_M) / (HILL_END_M - HILL_START_M), 0.0), 1.0) for i in range(161)
    ]}
    if gps:
        raw["latlng"] = {"data": [list(pt(0, i * 5.0)) for i in range(161)]}
    path = streams._raw_path(activity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(raw).encode()))


def build_map(conn: sqlite3.Connection) -> None:
    m = Map()
    m.line([(0, 0), (0, 400)], "residential", name="ridge_road")
    m.line([(0, 400), (0, 800)], "path", name="ridge_trail")
    m.build(conn)
    insert_area(conn)


def test_a_short_steep_hill_keeps_its_grade_and_each_source_is_labelled(conn, server, monkeypatch):
    write_run(1)
    monkeypatch.setattr(elevation, "sampler_for", lambda c: climbing_sampler(step_m=1.0))

    result = call_tool(server, "get_activity_stretch", {"activity_id": 1, "start_m": 190, "end_m": 270})

    recorded = result["recorded_altitude"]
    assert recorded["gain_m"] == pytest.approx(HILL_RISE_M, abs=1.0) and recorded["loss_m"] == 0
    assert recorded["avg_grade_pct"] == pytest.approx(15.0, abs=1.0)  # 12 m over 80 m; a 50 m smoothing would flatten it
    assert result["map_elevation"]["avg_grade_pct"] == pytest.approx(17.5, abs=0.5)
    assert "recorded altitude" in recorded["source"] and "map elevation" in result["map_elevation"]["source"]
    assert result["length_m"] == 80 and result["start"]["lat"] == pytest.approx(pt(0, 190)[0])
    assert result["start"]["lng"] == pytest.approx(pt(0, 190)[1], abs=1e-5)


def test_the_whole_run_lists_its_ways_in_order_with_ranges_and_a_capped_profile(conn, server):
    write_run(1)
    build_map(conn)

    whole = call_tool(server, "get_activity_stretch", {"activity_id": 1})
    part = call_tool(server, "get_activity_stretch", {"activity_id": 1, "start_m": 100, "end_m": 200})

    assert [(w["name"], w["highway"]) for w in whole["ways"]] == [("ridge_road", "residential"), ("ridge_trail", "path")]
    first, second = whole["ways"]
    assert first["start_m"] == 0 and first["end_m"] == pytest.approx(400, abs=10)
    assert second["start_m"] == pytest.approx(400, abs=10) and second["end_m"] == 800
    assert whole["area"] == "test" and len(whole["points"]) <= 40
    assert [w["name"] for w in part["ways"]] == ["ridge_road"]
    assert part["ways"][0]["start_m"] == 100 and part["ways"][0]["end_m"] == 200


def test_without_an_imported_area_the_ways_are_empty_and_unfilled_map_elevation_is_null(conn, server, monkeypatch):
    write_run(1)
    monkeypatch.setattr(elevation, "sampler_for", lambda c: none_sampler())

    result = call_tool(server, "get_activity_stretch", {"activity_id": 1, "start_m": 0, "end_m": 100})

    assert result["ways"] == [] and result["area"] is None and "No imported area" in result["ways_note"]
    assert result["map_elevation"] is None and result["points"][0]["map_elevation_m"] is None
    assert result["recorded_altitude"] is not None


def test_a_run_without_streams_or_gps_errors_plainly(conn, server):
    write_run(2, gps=False)

    assert "No cached streams" in call_tool(server, "get_activity_stretch", {"activity_id": 1})["error"]
    assert "no GPS" in call_tool(server, "get_activity_stretch", {"activity_id": 2})["error"]
