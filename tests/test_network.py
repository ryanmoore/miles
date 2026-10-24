import sqlite3

import pytest
from helpers import FakeSampler, by_deg_lat, by_deg_lng, call_tool, climbing_sampler, node, none_sampler, way
from mcp.server.fastmcp import FastMCP

from miles import network, routing_mcp, terrain


# A small synthetic tile set: two contiguous "busy" secondary-road ways
# (A, B) sharing a junction, a footway (C) continuing from that junction
# and running near a motorway (E, geometry-only), a quiet residential road
# (D) with a drinking-water node beside it, and a private path (F) that
# must be excluded entirely. Way A is duplicated verbatim across both
# payloads to model the overlapping-tile case.
NODE10 = (40.0, -105.0)
NODE11 = (40.0 + by_deg_lat(600.0), -105.0)
NODE12 = (NODE11[0] + by_deg_lat(600.0), -105.0)
NODE21 = (NODE12[0] + by_deg_lat(130.0), -105.0)
NODE30 = (41.0, -105.0)
NODE31 = (41.0 + by_deg_lat(150.0), -105.0)
NODE40 = (40.0108, -104.9995)
NODE41 = (40.0120, -104.9995)
NODE50 = (42.0, -105.0)
NODE51 = (42.001, -105.0)
NODE60 = (41.0007, -105.0)

WAY_A_ID, WAY_B_ID, WAY_C_ID, WAY_D_ID, WAY_E_ID, WAY_F_ID = 1, 2, 3, 4, 5, 6


def _way_a() -> dict:
    return way(WAY_A_ID, [10, 11], {"highway": "secondary", "maxspeed": "45 mph", "name": "Busy Rd A"})


def _way_b() -> dict:
    return way(WAY_B_ID, [11, 12], {"highway": "secondary", "maxspeed": "45 mph", "name": "Busy Rd B"})


def _way_c() -> dict:
    return way(WAY_C_ID, [12, 21], {"highway": "footway", "name": "Quiet Trail"})


def _synthetic_payloads() -> list[dict]:
    payload1 = {
        "elements": [
            node(10, *NODE10), node(11, *NODE11), node(12, *NODE12),
            _way_a(), _way_b(),
            node(40, *NODE40), node(41, *NODE41), way(WAY_E_ID, [40, 41], {"highway": "motorway", "name": "Freeway"}),
        ]
    }
    payload2 = {
        "elements": [
            _way_a(),  # same way id as payload1 -- tile overlap, must dedupe
            node(21, *NODE21), _way_c(),
            node(30, *NODE30), node(31, *NODE31), way(WAY_D_ID, [30, 31], {"highway": "residential", "name": "Quiet Rd"}),
            node(60, *NODE60, tags={"amenity": "drinking_water"}),
            node(50, *NODE50), node(51, *NODE51), way(WAY_F_ID, [50, 51], {"highway": "path", "access": "private"}),
        ]
    }
    return [payload1, payload2]


def _edge_id_for_way(conn: sqlite3.Connection, way_id: int) -> int:
    row = conn.execute("SELECT edge_id FROM net_edges WHERE way_id = ? ORDER BY edge_id", [way_id]).fetchone()
    assert row is not None, f"no edge for way {way_id}"
    return int(row["edge_id"])


def _build(conn: sqlite3.Connection, area_id: int = 1, sampler=None):
    return network.build_network(conn, area_id, _synthetic_payloads(), sampler or climbing_sampler())


def _score(conn: sqlite3.Connection, edge_ids: list[int], area_id: int = 1) -> dict:
    prefs = network.get_route_preferences(conn)
    return network.score_path(conn, area_id, [(e, True) for e in edge_ids], prefs, terrain.default_model().score_profile)


def test_build_network_dedupes_overlapping_tiles_excludes_private_ways_counts_every_node_and_sets_edge_attributes(conn: sqlite3.Connection):
    stats = _build(conn)
    assert stats["excluded_way_count"] == 1  # way F only

    row = conn.execute("SELECT * FROM net_graphs WHERE area_id = 1").fetchone()
    assert row["derive_version"] == network.NETWORK_DERIVE_VERSION
    assert row["edge_count"] == stats["edge_count"]
    # nodes spliced in for amenities count too.
    actual = conn.execute("SELECT COUNT(*) FROM net_nodes WHERE area_id = 1").fetchone()[0]
    assert stats["node_count"] == actual == row["node_count"]

    # way A appeared in both payloads verbatim; exactly one edge results.
    assert conn.execute("SELECT COUNT(*) AS n FROM net_edges WHERE way_id = ?", [WAY_A_ID]).fetchone()["n"] == 1
    # the private way never becomes an edge.
    assert conn.execute("SELECT COUNT(*) AS n FROM net_edges WHERE way_id = ?", [WAY_F_ID]).fetchone()["n"] == 0
    # the motorway is excluded from routing too.
    assert conn.execute("SELECT COUNT(*) AS n FROM net_edges WHERE way_id = ?", [WAY_E_ID]).fetchone()["n"] == 0

    kinds = {r["osm_node_id"]: r["kind"] for r in conn.execute("SELECT osm_node_id, kind FROM net_nodes WHERE area_id = 1")}
    assert kinds[11] == "junction"  # shared by ways A and B
    assert kinds[10] == "endpoint"  # only ends way A
    assert kinds[12] == "junction"  # shared by a secondary road and a footway

    edge_a = conn.execute("SELECT * FROM net_edges WHERE way_id = ?", [WAY_A_ID]).fetchone()
    assert edge_a["surface_class"] == "paved"
    assert edge_a["maxspeed_mph"] == pytest.approx(45.0)
    assert edge_a["maxspeed_filled"] == 0
    assert edge_a["has_elevation"] == 1

    # Way D is split into two edges at the drinking-water node, which sits
    # partway along it -- an amenity is spliced into the network, not just
    # annotated onto the edge it's nearest to.
    edge_d_rows = conn.execute("SELECT * FROM net_edges WHERE way_id = ? ORDER BY edge_id", [WAY_D_ID]).fetchall()
    assert len(edge_d_rows) == 2
    for edge_d in edge_d_rows:
        assert edge_d["maxspeed_filled"] == 1  # no maxspeed tag -> filled from the class default
        assert edge_d["maxspeed_mph"] == pytest.approx(25.0)

    edge_c = conn.execute("SELECT * FROM net_edges WHERE way_id = ?", [WAY_C_ID]).fetchone()
    assert edge_c["surface_class"] == "trail"

    amenity = conn.execute("SELECT * FROM net_nodes WHERE area_id = 1 AND kind = 'drinking_water'").fetchone()
    # the two D pieces share the amenity node between them.
    assert edge_d_rows[0]["to_node_id"] == edge_d_rows[1]["from_node_id"] == amenity["node_id"]


def test_way_override_flips_tier_and_avoid_is_recorded(conn: sqlite3.Connection):
    _build(conn)
    edge_a = _edge_id_for_way(conn, WAY_A_ID)
    edge_c = _edge_id_for_way(conn, WAY_C_ID)
    edge_d = _edge_id_for_way(conn, WAY_D_ID)

    network.set_way_override(conn, WAY_A_ID, "comfortable")
    score = _score(conn, [edge_a])
    assert score["road_exposure"]["totals_m"]["comfortable"] == pytest.approx(score["distance_m"])
    assert score["road_exposure"]["totals_m"]["uncomfortable"] == 0.0

    network.set_way_override(conn, WAY_D_ID, "avoid")
    network.set_way_override(conn, WAY_C_ID, "avoid")  # an avoided trail is reported too
    score_d = _score(conn, [edge_d])
    assert score_d["road_exposure"]["totals_m"]["uncomfortable"] == pytest.approx(score_d["distance_m"])
    assert score_d["road_exposure"]["avoided_ways"] == [WAY_D_ID]
    assert _score(conn, [edge_c])["road_exposure"]["avoided_ways"] == [WAY_C_ID]
    assert network.avoided_way_ids(conn) == {WAY_C_ID, WAY_D_ID}


def test_stateful_road_exposure_flags_one_long_run_but_not_two_short_ones(conn: sqlite3.Connection):
    _build(conn)
    prefs = network.get_route_preferences(conn)
    edge_a = _edge_id_for_way(conn, WAY_A_ID)
    edge_b = _edge_id_for_way(conn, WAY_B_ID)
    edge_c = _edge_id_for_way(conn, WAY_C_ID)

    len_a = conn.execute("SELECT length_m FROM net_edges WHERE edge_id = ?", [edge_a]).fetchone()["length_m"]
    len_b = conn.execute("SELECT length_m FROM net_edges WHERE edge_id = ?", [edge_b]).fetchone()["length_m"]
    assert len_a < prefs["max_continuous_uncomfortable_m"] < len_a + len_b

    # A and B run back-to-back with no break -- one long uncomfortable run.
    exposure = _score(conn, [edge_a, edge_b])["road_exposure"]
    assert len(exposure["violations"]) == 1
    assert exposure["max_continuous_uncomfortable_m"] == pytest.approx(len_a + len_b)

    # The same two uncomfortable edges split by trail (edge C) -- two short
    # runs, neither over the threshold, so no violation.
    split_exposure = _score(conn, [edge_a, edge_c, edge_b])["road_exposure"]
    assert split_exposure["violations"] == []
    assert split_exposure["max_continuous_uncomfortable_m"] == pytest.approx(max(len_a, len_b))
    assert split_exposure["totals_m"]["uncomfortable"] == pytest.approx(len_a + len_b)


def test_amenity_tagged_on_a_shared_junction_node_reuses_it(conn: sqlite3.Connection):
    """A trailhead or parking tag can sit directly on a node that's also a
    way junction (real OSM data does this). net_nodes is unique on
    (area_id, osm_node_id), so build_network must reuse that node rather
    than try to insert a second row under the same osm_node_id."""
    payload = {
        "elements": [
            node(10, *NODE10), node(11, *NODE11),
            node(12, *NODE12, tags={"amenity": "parking"}),
            node(21, *NODE21),
            _way_a(), _way_b(), _way_c(),
        ]
    }
    stats = network.build_network(conn, 1, [payload], climbing_sampler())
    assert stats["node_count"] > 0

    rows = conn.execute("SELECT * FROM net_nodes WHERE area_id = 1 AND osm_node_id = 12").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "parking"

    # Reusing the node means no edge was cut for the amenity: one edge per
    # way, and none looping back to its own start.
    assert conn.execute("SELECT COUNT(*) FROM net_edges WHERE area_id = 1").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM net_edges WHERE from_node_id = to_node_id").fetchone()[0] == 0

    edge_b = _edge_id_for_way(conn, WAY_B_ID)
    edge_c = _edge_id_for_way(conn, WAY_C_ID)
    assert conn.execute("SELECT to_node_id FROM net_edges WHERE edge_id = ?", [edge_b]).fetchone()["to_node_id"] == rows[0]["node_id"]
    assert conn.execute("SELECT from_node_id FROM net_edges WHERE edge_id = ?", [edge_c]).fetchone()["from_node_id"] == rows[0]["node_id"]


def test_get_and_set_route_preferences(conn: sqlite3.Connection):
    assert network.get_route_preferences(conn) == network.DEFAULT_PREFERENCES

    updated = network.set_route_preferences(conn, {"max_continuous_uncomfortable_m": 400.0})
    assert updated["max_continuous_uncomfortable_m"] == 400.0
    # unrelated defaults survive a partial patch.
    assert updated["road_comfort"]["comfortable_max_mph"] == network.DEFAULT_PREFERENCES["road_comfort"]["comfortable_max_mph"]

    assert network.get_route_preferences(conn)["max_continuous_uncomfortable_m"] == 400.0


@pytest.mark.parametrize(
    "call",
    [
        lambda conn: network.set_route_preferences(conn, {"not_a_real_key": 1}),
        lambda conn: network.set_route_preferences(conn, {"road_comfort": {"comfortable_max_mph": 50.0, "uncomfortable_min_mph": 40.0}}),
        lambda conn: network.set_way_override(conn, 1, "meh"),
    ],
    ids=["unknown key", "invalid threshold order", "bad verdict"],
)
def test_invalid_preferences_and_overrides_are_rejected(conn: sqlite3.Connection, call):
    with pytest.raises(ValueError):
        call(conn)


# ---------------------------------------------------------------------------
# Sidewalk classification: footway is OSM's catch-all for both a genuine
# natural-surface trail and a street's sidewalk or crossing. build_network
# tells them apart at build time -- tag rules first, a geometric fallback
# (alongside a road-like way) for whatever isn't tagged either way -- so
# every consumer of surface_class (route search, drive-start ranking) sees
# the distinction without redoing the classification itself.
# ---------------------------------------------------------------------------

_FOOTWAY_ID = 90001


def _footway_surface_class(conn: sqlite3.Connection, footway_tags: dict, nearby_road: dict | None = None, crossing: bool = False) -> str:
    """The class build_network gives a 300 m footway running east, with a
    road of the given tags either 10 m beside it or (crossing) cutting
    across its middle."""
    lat0, lng0 = 7.0, 12.0
    end_lng = lng0 + by_deg_lng(lat0, 300.0)
    elements = [node(9001, lat0, lng0), node(9002, lat0, end_lng), way(_FOOTWAY_ID, [9001, 9002], footway_tags)]
    if nearby_road is not None and not crossing:
        side = lat0 + by_deg_lat(10.0)
        elements += [node(9011, side, lng0), node(9012, side, end_lng), way(90002, [9011, 9012], nearby_road)]
    if nearby_road is not None and crossing:
        mid = lng0 + by_deg_lng(lat0, 150.0)
        elements += [
            node(9021, lat0 - by_deg_lat(200.0), mid), node(9022, lat0 + by_deg_lat(200.0), mid), way(90003, [9021, 9022], nearby_road)
        ]
    network.build_network(conn, 101, [{"elements": elements}], none_sampler())
    return conn.execute("SELECT surface_class FROM net_edges WHERE area_id = 101 AND way_id = ?", [_FOOTWAY_ID]).fetchone()["surface_class"]


@pytest.mark.parametrize(
    "footway_tags, nearby_road, crossing, expected",
    [
        ({"highway": "footway", "footway": "sidewalk"}, None, False, "sidewalk"),
        ({"highway": "footway", "footway": "crossing"}, None, False, "sidewalk"),
        ({"highway": "footway", "footway": "traffic_island"}, None, False, "sidewalk"),
        ({"highway": "footway", "sidewalk": "yes"}, None, False, "sidewalk"),
        ({"highway": "footway", "crossing": "marked"}, None, False, "sidewalk"),
        # A sidewalk is paved as often as not: the tag applies regardless of surface.
        ({"highway": "footway", "footway": "sidewalk", "surface": "asphalt"}, None, False, "sidewalk"),
        ({"highway": "footway", "name": "Backcountry Path"}, None, False, "trail"),
        ({"highway": "footway"}, {"highway": "residential", "name": "Elm Street"}, False, "sidewalk"),
        ({"highway": "footway"}, {"highway": "primary"}, False, "sidewalk"),
        ({"highway": "footway", "name": "Ridge Path"}, {"highway": "track"}, False, "trail"),
        # A trail that merely crosses a road, rather than paralleling it, stays a trail.
        ({"highway": "footway", "name": "Ridge Trail"}, {"highway": "residential", "name": "Cross Street"}, True, "trail"),
    ],
    ids=[
        "sidewalk subtag", "crossing subtag", "traffic island subtag", "sidewalk=yes", "crossing=marked", "paved sidewalk",
        "no road nearby", "beside a residential road", "beside a primary road", "beside a fire road", "crossing a road",
    ],
)
def test_footway_classification_by_tag_and_by_geometry(conn: sqlite3.Connection, footway_tags, nearby_road, crossing, expected):
    assert _footway_surface_class(conn, footway_tags, nearby_road, crossing) == expected


# ---------------------------------------------------------------------------
# Tag parsing, comfort tiers, build and scoring edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [("50;30", 50 * 0.621371), ("signals", None)], ids=["a bare number is km/h and the first of a list wins", "not a number"])
def test_parse_maxspeed_mph(raw, expected):
    parsed = network._parse_maxspeed_mph(raw)
    assert parsed is None if expected is None else parsed == pytest.approx(expected, rel=1e-4)


@pytest.mark.parametrize(
    "highway, tags, expected",
    [
        ("residential", {"oneway": "yes"}, "no"),
        ("secondary", {"oneway": "-1"}, "no"),
        ("residential", {"oneway": "yes", "oneway:foot": "yes"}, "yes"),
        ("residential", {"oneway": "yes", "oneway:foot": "no"}, "no"),
        ("residential", {"oneway:foot": "-1"}, "-1"),
        ("path", {"oneway": "yes"}, "yes"),
        ("track", {"oneway": "-1"}, "-1"),
        ("footway", {"oneway": "yes", "oneway:foot": "no"}, "no"),
        ("path", {"oneway:bicycle": "yes"}, "no"),
        ("path", {"oneway": "no", "oneway:bicycle": "yes"}, "no"),
    ],
    ids=[
        "a one-way street is two-way for a runner", "a reversed one-way street too", "oneway:foot binds a street",
        "oneway:foot=no", "oneway:foot=-1", "a one-way path binds", "a one-way track binds",
        "oneway:foot overrides oneway on a path", "oneway:bicycle is ignored", "oneway:bicycle beside oneway=no",
    ],
)
def test_the_runner_direction_of_a_way_follows_foot_tags_and_ignores_street_and_bicycle_oneways(highway, tags, expected):
    assert network._oneway_value(highway, tags) == expected


def _road_exposure(conn: sqlite3.Connection, tags: dict) -> tuple[dict, float]:
    """Per-tier metres and total distance for a 300 m road with the given tags."""
    elements = [node(1, 40.0, -105.0), node(2, 40.0 + by_deg_lat(300.0), -105.0), way(10, [1, 2], tags)]
    network.build_network(conn, 1, [{"elements": elements}], none_sampler())
    score = _score(conn, [_edge_id_for_way(conn, 10)])
    return score["road_exposure"]["totals_m"], score["distance_m"]


@pytest.mark.parametrize(
    "tags, expected_tier",
    [
        ({"highway": "residential"}, "comfortable"),
        ({"highway": "residential", "lanes": "3"}, "tolerable"),
        ({"highway": "tertiary", "maxspeed": "55 mph", "shoulder": "yes"}, "uncomfortable"),  # a shoulder is not separation
        ({"highway": "secondary"}, "uncomfortable"),  # untagged speed is filled from the class
        ({"highway": "secondary", "maxspeed": "35 mph", "sidewalk": "both"}, "tolerable"),  # a sidewalk lifts one tier
        ({"highway": "secondary", "maxspeed": "35 mph", "cycleway": "track"}, "tolerable"),
        ({"highway": "secondary", "maxspeed": "35 mph", "sidewalk": "no"}, "uncomfortable"),
        ({"highway": "tertiary", "maxspeed": "55 mph", "sidewalk": "both"}, "tolerable"),  # a sidewalk lifts a fast road one tier, never to comfortable
    ],
    ids=[
        "residential", "residential with 3 lanes", "fast road with a shoulder", "untagged speed", "sidewalk lifts a tier",
        "cycle track lifts a tier", "no sidewalk", "sidewalk on a fast road",
    ],
)
def test_a_road_is_scored_into_a_comfort_tier_from_its_tags(conn: sqlite3.Connection, tags: dict, expected_tier: str):
    totals, distance_m = _road_exposure(conn, tags)
    assert totals[expected_tier] == pytest.approx(distance_m)


def test_score_path_reports_gain_runnability_and_partial_elevation_coverage_honestly(conn: sqlite3.Connection):
    south_only = FakeSampler(lambda i, lat, lng: 100.0 + i if lat < 40.5 else None)  # only the southern half has elevation
    network.build_network(conn, 1, _synthetic_payloads(), south_only)
    edge_a = _edge_id_for_way(conn, WAY_A_ID)
    edge_d = _edge_id_for_way(conn, WAY_D_ID)

    full = _score(conn, [edge_a])
    assert full["has_elevation"] is True
    assert full["elevation_coverage_pct"] == pytest.approx(100.0)
    assert full["gain_m"] is not None and full["gain_m"] > 0
    runnability = full["runnability"]
    assert runnability["run_pct"] + runnability["grind_pct"] + runnability["hike_pct"] == pytest.approx(100.0)

    partial = _score(conn, [edge_a, edge_d])
    assert partial["has_elevation"] is False
    assert 0.0 < partial["elevation_coverage_pct"] < 100.0

    none = _score(conn, [edge_d])
    assert none["has_elevation"] is False
    assert none["gain_m"] is None
    assert none["elevation_coverage_pct"] == 0.0


TRAIL_START = (40.0, -105.0)
TRAIL_END = (40.0 + by_deg_lat(400.0), -105.0)
SETBACK_LOT = (40.0 + by_deg_lat(200.0), -105.0 + by_deg_lng(40.0, 40.0))


def _setback_lot_payload(real_node_id: int, lot_id: int, lot_kind_tags: dict | None = None) -> list[dict]:
    """A trail whose end node has `real_node_id`, and a parking lot with
    amenity id `lot_id` sitting 40 m off its middle."""
    return [{
        "elements": [
            node(real_node_id, *TRAIL_START), node(9001, *TRAIL_END),
            way(100, [real_node_id, 9001], {"highway": "path"}),
            node(lot_id, *SETBACK_LOT, tags=lot_kind_tags or {"amenity": "parking"}),
        ]
    }]


def test_set_back_amenities_never_collide_on_a_node_id(conn: sqlite3.Connection):
    # A lot mapped as an outline carries the negated way id as its amenity id;
    # the trail happens to use a node whose id equals that way id.
    network.build_network(conn, 1, _setback_lot_payload(real_node_id=7777, lot_id=-7777), climbing_sampler())
    ids = [r[0] for r in conn.execute("SELECT osm_node_id FROM net_nodes WHERE area_id = 1")]
    assert len(ids) == len(set(ids))
    assert {7777, -7777} <= set(ids)

    # Two set-back amenities with opposite-signed ids stay distinct junctions.
    payload = _setback_lot_payload(real_node_id=8000, lot_id=7777)
    payload[0]["elements"].append(node(-7777, SETBACK_LOT[0] + by_deg_lat(60.0), SETBACK_LOT[1], tags={"amenity": "parking"}))
    network.build_network(conn, 2, payload, climbing_sampler())
    ids = [r[0] for r in conn.execute("SELECT osm_node_id FROM net_nodes WHERE area_id = 2")]
    assert len(ids) == len(set(ids))


def test_a_failed_build_leaves_the_previous_graph_in_place(conn: sqlite3.Connection):
    network.build_network(conn, 1, _synthetic_payloads(), climbing_sampler())
    before = conn.execute("SELECT COUNT(*) FROM net_edges WHERE area_id = 1").fetchone()[0]
    built_at = conn.execute("SELECT built_at FROM net_graphs WHERE area_id = 1").fetchone()[0]

    class FailingSampler:
        def sample(self, points):
            raise RuntimeError("dem unavailable")

    with pytest.raises(RuntimeError):
        network.build_network(conn, 1, _synthetic_payloads(), FailingSampler())
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM net_edges WHERE area_id = 1").fetchone()[0] == before
    assert conn.execute("SELECT built_at FROM net_graphs WHERE area_id = 1").fetchone()[0] == built_at


def _spur_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM net_edges WHERE area_id = 1 AND way_id < 0").fetchall()


def test_an_access_spur_has_elevation_and_counts_no_trail_for_it(conn: sqlite3.Connection):
    network.build_network(conn, 1, _setback_lot_payload(real_node_id=8000, lot_id=5555), climbing_sampler())
    (spur,) = _spur_rows(conn)
    assert spur["has_elevation"] == 1
    assert spur["surface_class"] != "trail"
    assert not network.is_trail_class(spur["highway"], spur["surface_class"])

    score = _score(conn, [spur["edge_id"]])
    assert score["has_elevation"] is True
    assert score["elevation_coverage_pct"] == pytest.approx(100.0)
    assert score["trail_distance_m"] == 0.0
    assert score["paved_distance_m"] == 0.0
    assert score["connector_distance_m"] == pytest.approx(score["distance_m"])


@pytest.mark.parametrize(
    "setback_m, spliced",
    [(network.AMENITY_SNAP_RADIUS_M - 10.0, True), (network.AMENITY_SNAP_RADIUS_M + 10.0, False)],
    ids=["inside the snap radius", "outside the snap radius"],
)
def test_an_amenity_is_spliced_into_an_edge_only_within_the_snap_radius(conn: sqlite3.Connection, setback_m: float, spliced: bool):
    lot = (TRAIL_START[0] + by_deg_lat(200.0), TRAIL_START[1] + by_deg_lng(40.0, setback_m))
    payload = [{
        "elements": [
            node(8000, *TRAIL_START), node(9001, *TRAIL_END),
            way(100, [8000, 9001], {"highway": "path"}),
            node(5555, *lot, tags={"amenity": "parking"}),
        ]
    }]
    network.build_network(conn, 1, payload, climbing_sampler())
    assert (len(_spur_rows(conn)) == 1) is spliced
    trail_pieces = conn.execute("SELECT COUNT(*) FROM net_edges WHERE area_id = 1 AND way_id = 100").fetchone()[0]
    assert trail_pieces == (2 if spliced else 1)


def test_several_amenities_on_one_edge_chain_the_pieces_end_to_end(conn: sqlite3.Connection):
    lots = [(5551, 100.0), (5552, 250.0), (5553, 40.0)]
    elements = [
        node(8000, *TRAIL_START), node(9001, *TRAIL_END),
        way(100, [8000, 9001], {"highway": "path"}),
    ] + [
        node(lot_id, TRAIL_START[0] + by_deg_lat(along_m), TRAIL_START[1] + by_deg_lng(40.0, 30.0), tags={"amenity": "parking"})
        for lot_id, along_m in lots
    ]
    network.build_network(conn, 1, [{"elements": elements}], climbing_sampler())

    trail = conn.execute(
        "SELECT e.length_m, a.osm_node_id AS from_osm, b.osm_node_id AS to_osm FROM net_edges e "
        "JOIN net_nodes a ON a.node_id = e.from_node_id JOIN net_nodes b ON b.node_id = e.to_node_id "
        "WHERE e.area_id = 1 AND e.way_id = 100"
    ).fetchall()
    assert len(trail) == 4
    assert sum(r["length_m"] for r in trail) == pytest.approx(400.0, abs=1.0)
    starts = {r["from_osm"] for r in trail}
    ends = {r["to_osm"] for r in trail}
    assert starts - ends == {8000} and ends - starts == {9001}


def test_the_preferences_tool_applies_a_patch_and_reports_a_rejected_one_as_an_error():
    server = FastMCP("test")
    routing_mcp.register_routing_tools(server)

    assert call_tool(server, "set_route_preferences", {"patch": {"max_continuous_uncomfortable_m": 1000}})["max_continuous_uncomfortable_m"] == 1000
    assert call_tool(server, "set_route_preferences", {"patch": '{"max_continuous_uncomfortable_m": 900}'})["max_continuous_uncomfortable_m"] == 900
    assert "unknown preference key" in call_tool(server, "set_route_preferences", {"patch": {"surface_weight": {}}})["error"]
    assert "positive" in call_tool(server, "set_route_preferences", {"patch": {"max_continuous_uncomfortable_m": -1}})["error"]


def test_a_footpath_width_unpaved_track_counts_as_singletrack_and_trail_not_unpaved_road(conn: sqlite3.Connection):
    payload = [{"elements": [
        node(70, 40.0, -105.0), node(71, 40.001, -105.0),
        way(70, [70, 71], {"highway": "track", "surface": "dirt", "width": "1"}),
    ]}]
    network.build_network(conn, 1, payload, climbing_sampler())
    score = _score(conn, [_edge_id_for_way(conn, 70)])
    assert score["singletrack_distance_m"] == pytest.approx(score["distance_m"])
    assert score["trail_distance_m"] == pytest.approx(score["distance_m"])
    assert score["unpaved_road_distance_m"] == 0.0
