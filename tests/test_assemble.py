import itertools
import math
import sqlite3

import pytest
from helpers import ORIGIN_LAT, FakeSampler, by_deg_lat
from test_sections import Map, pt

from miles import assemble, route_search, routing_mcp, sections, survey

START = pt(0, 0)


@pytest.fixture
def keep_similar(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests that compare how the candidates on one stretch of ground score."""
    monkeypatch.setattr(route_search, "SIMILAR_SHARE", 1.0)


@pytest.fixture(autouse=True)
def no_fragment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "FRAGMENT_MIN_M", 0.0)
    monkeypatch.setattr(survey, "FRAGMENT_RUN_SHARE", 0.0)


def request(start: tuple[float, float], dmin: float, dmax: float, **extra) -> route_search.RouteSearchRequest:
    req: route_search.RouteSearchRequest = {
        "area_id": 1,
        "starts": [{"lat": start[0], "lng": start[1]}],
        "distance_m_min": dmin,
        "distance_m_max": dmax,
        "n": 10,
        "time_budget_s": 20.0,
        "seed": 1,
    }
    req.update(extra)  # type: ignore[typeddict-item]
    return req


def group_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {s.name: s.section_id for s in sections.load_sections(conn, 1, level=sections.GROUP)}


def used(candidate: route_search.RouteCandidateOut) -> set[str]:
    """The names of the sections a candidate runs, a stretch's name being its section's."""
    return {u["name"].split(":")[0] for u in candidate["sections_used"]}


def named(result: route_search.RouteSearchResult, shape: str | None = None) -> list[route_search.RouteCandidateOut]:
    return [c for c in result["candidates"] if shape is None or c["shape"] == shape]


MILE = 1609.344


def way_names(candidate: route_search.RouteCandidateOut) -> list[str]:
    return [w["name"] for w in candidate["ways"]]


def ring_with_street() -> Map:
    """A 2400 m trail square reached by a 300 m quiet street."""
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.square(0, 300, 600, name="Ridge Loop")
    return m


def test_an_out_and_back_along_a_block_turns_where_the_distance_fits(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.line([(0, 300), (0, 4300)], "path", name="Long Trail")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 2800, 3200, shape="out_and_back"))

    assert result["candidates"]
    for c in result["candidates"]:
        assert c["built_from"] == "sections" and c["shape"] == "out_and_back"
        assert 2800 <= c["distance_m"] <= 3200  # turned partway along 4000 m of trail, not at its end
        assert used(c) == {"Long Trail"}
        assert not c["missed_constraints"]


def test_a_walk_that_retraces_only_its_access_is_a_lollipop_and_one_that_does_not_is_a_loop(conn: sqlite3.Connection):
    ring_with_street().build(conn)

    result = route_search.find_routes(conn, request(START, 2700, 3300))

    assert named(result, "lollipop") and not named(result, "loop")
    by_way: dict[str, float] = {}
    for w in named(result, "lollipop")[0]["ways"]:
        by_way[w["name"]] = by_way.get(w["name"], 0.0) + w["length_m"]
    assert by_way["Access Street"] == pytest.approx(600, rel=0.05)  # in and out once each
    assert by_way["Ridge Loop"] == pytest.approx(2400, rel=0.05)

    m = Map()
    m.square(0, 0, 600, name="Ridge Loop")
    m.build(conn, area_id=2)
    loops = named(route_search.find_routes(conn, {**request(START, 2200, 2600, shape="loop"), "area_id": 2}), "loop")
    assert loops and way_names(loops[0]) == ["Ridge Loop"] * len(way_names(loops[0]))
    assert sum(w["length_m"] for w in loops[0]["ways"]) == pytest.approx(2400, rel=0.05)  # every side once


# --- stitched loops ----------------------------------------------------------


def two_trails_to_a_summit(one_way: str | None = None) -> Map:
    """Two 2 x 1600 m trails from a trailhead meeting at a summit, a street from
    the start to the trailhead and a longer one back. Trails are cut into two
    stretches each by a name change. `one_way` makes the access street or the
    second trail one-way, in the direction that leaves the loop runnable."""
    m = Map()
    access = {"oneway:foot": "yes"} if one_way == "street" else {}
    descent = {"oneway": "-1"} if one_way == "trail" else {}
    m.line([(0, 0), (1000, 0)], "residential", name="Access Street", maxspeed="25 mph", **access)
    m.line([(1000, 0), (1000, -500), (0, -500), (0, 0)], "residential", name="Return Street", maxspeed="25 mph")
    m.line([(1000, 0), (1000, 1600)], "path", name="Alder Trail")
    m.line([(1000, 1600), (1000, 3200)], "path", name="Alder Ridge")
    m.line([(1000, 0), (2000, 0), (2000, 1600)], "path", name="Birch Trail", **descent)
    m.line([(2000, 1600), (2000, 3200), (1000, 3200)], "path", name="Birch Ridge", **descent)
    return m


@pytest.mark.parametrize("one_way", [None, "street", "trail"])
def test_the_top_route_climbs_one_trail_and_descends_the_other_rather_than_retracing_one(conn: sqlite3.Connection, one_way: str | None):
    two_trails_to_a_summit(one_way).build(conn)

    result = route_search.find_routes(conn, request(START, 10_500, 12_300))

    top = result["candidates"][0]
    assert top["shape"] == "loop" and 10_500 <= top["distance_m"] <= 12_300
    assert {"Alder Trail", "Alder Ridge", "Birch Ridge", "Birch Trail"} <= set(way_names(top))
    assert all(way_names(top).count(trail) == 1 for trail in ("Alder Trail", "Alder Ridge", "Birch Ridge", "Birch Trail"))


def test_a_loop_stitches_three_trails_that_meet_end_to_end(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (500, 0)], "residential", name="Access Street", maxspeed="25 mph")
    m.line([(500, 0), (500, 2500)], "path", name="Alder Trail")
    m.line([(500, 2500), (2000, 2500)], "path", name="Birch Trail")
    m.line([(2000, 2500), (2000, 0), (500, 0)], "path", name="Cedar Trail")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 8_800, 9_400))

    top = result["candidates"][0]
    assert {"Alder Trail", "Birch Trail", "Cedar Trail"} <= set(way_names(top))
    assert top["shape"] == "lollipop" and 8_800 <= top["distance_m"] <= 9_400
    assert max(w["length_m"] for w in top["ways"] if w["name"] != "Access Street") <= 4_100  # no trail is run twice


# --- stops -------------------------------------------------------------------


def test_a_stop_at_the_start_makes_a_figure_eight_that_passes_it_near_the_mile(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 3400, name="West Loop")
    m.square(0, -3400, 3400, name="East Loop")
    m.build(conn)

    result = route_search.find_routes(
        conn, request(START, 16.5 * MILE, 17.5 * MILE, stops=[{"lat": START[0], "lng": START[1], "at_mi": 8}])
    )

    top = result["candidates"][0]
    assert 16.5 * MILE <= top["distance_m"] <= 17.5 * MILE
    assert abs(top["stops"][0]["at_mi"] - 8) <= 1.0
    assert all(any(loop in name for name in used(top)) for loop in ("West Loop", "East Loop"))


def test_a_stop_away_from_the_start_is_passed_in_its_window_and_reported(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (2000, 0)], "residential", name="Lot Street", maxspeed="25 mph")
    m.square(2000, 0, 1500, name="Lot Loop")
    m.build(conn)
    lot = pt(2000, 0)

    result = route_search.find_routes(
        conn, request(START, 11_000, 13_000, stops=[{"lat": lot[0], "lng": lot[1], "label": "lot", "at_mi": 3.5}])
    )

    assert result["candidates"]
    for c in result["candidates"]:
        assert c["stops"][0]["label"] == "lot" and abs(c["stops"][0]["at_mi"] - 3.5) <= 1.0


def test_stops_that_no_section_can_serve_get_a_warning_not_a_walk(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 100, name="Tiny Loop")  # under the minimum section length
    m.build(conn)
    stop = pt(0, 50)

    result = route_search.find_routes(conn, request(START, 300, 500, stops=[{"lat": stop[0], "lng": stop[1]}]))

    assert result["candidates"] == [] and result["warnings"]
    with pytest.raises(ValueError, match="no network found near stop"):
        route_search.find_routes(conn, request(START, 300, 500, stops=[{"lat": 10.0, "lng": 10.0}]))


# --- repeats -----------------------------------------------------------------


def ring_loop_scores(conn: sqlite3.Connection, **extra) -> dict[str, float]:
    result = route_search.find_routes(conn, request(pt(0, 600), 1500, 1700, **extra))
    best: dict[str, float] = {}
    for c in result["candidates"]:
        best[c["shape"]] = max(best.get(c["shape"], -1e9), c["ranking"]["score"] / c["ranking"]["shape"])
    return best


@pytest.mark.usefixtures("keep_similar")
def test_repeat_value_sets_how_much_of_its_value_a_retraced_stretch_keeps(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 400, 400, name="Ridge Loop")
    m.build(conn)

    default = route_search.find_routes(conn, request(pt(0, 600), 1500, 1700))
    none = ring_loop_scores(conn, repeat_value=0.0)
    half = ring_loop_scores(conn)

    assert default["candidates"][0]["shape"] == "loop"
    assert none["out_and_back"] == pytest.approx(0.5 * none["loop"], rel=0.02)  # the way back earns nothing
    assert half["out_and_back"] == pytest.approx(0.75 * half["loop"], rel=0.02)
    with pytest.raises(ValueError, match="repeat_value"):
        route_search.find_routes(conn, request(START, 1500, 1700, repeat_value=1.5))


def test_a_better_out_and_back_still_beats_a_lesser_loop(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 200)], "residential", name="West Lane", maxspeed="25 mph")
    m.square(0, 200, 400, name="Plain Loop")
    m.line([(0, 0), (0, -200)], "residential", name="East Lane", maxspeed="25 mph")
    m.line([(0, -200), (0, -1200)], "path", surface="asphalt", name="Scenic Path")
    m.waterway([(40, -1300), (40, -100)])
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 1800, 2600))

    top = result["candidates"][0]
    assert top["shape"] == "out_and_back" and used(top) == {"Scenic Path"}
    loops = [c for c in result["candidates"] if c["shape"] in ("loop", "lollipop")]
    assert loops and used(loops[0]) == {"Plain Loop"} and loops[0]["ranking"]["score"] < top["ranking"]["score"]


@pytest.mark.usefixtures("keep_similar")
def test_a_loop_over_two_named_sections_is_offered_among_many_walks_of_one_that_outscore_it(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (300, 0)], "residential", name="Access Street", maxspeed="25 mph")
    for i, j in itertools.product(range(5), repeat=2):
        if j < 4:
            m.line([(300 + 500 * i, 500 * j), (300 + 500 * i, 500 * (j + 1))], "path", surface="asphalt", name="Grid Trail")
        if i < 4:
            m.line([(300 + 500 * i, 500 * j), (300 + 500 * (i + 1), 500 * j)], "path", surface="asphalt", name="Grid Trail")
    m.waterway([(250, -40), (2350, -40)])
    m.line([(2300, 2000), (2300, 2600)], "path", name="Link Path")
    m.line([(2300, 2600), (1500, 2600)], "path", name="Birch Trail")
    m.line([(1500, 2600), (0, 2600), (0, 0)], "residential", maxspeed="25 mph")  # unnamed: no section
    m.build(conn)
    ids = group_ids(conn)

    result = route_search.find_routes(conn, {**request(START, 6_000, 11_000, sections=[ids["Grid Trail"], ids["Birch Trail trail system"]]), "n": 6})

    loop = next(c for c in result["candidates"] if c["shape"] == "loop")
    assert {"Grid Trail", "Birch Trail trail system"} <= used(loop)
    assert loop["ranking"]["score"] < result["candidates"][0]["ranking"]["score"]


def test_an_excluded_sections_ways_are_never_used_and_naming_it_is_refused(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="West Street", maxspeed="25 mph")
    m.square(0, 300, 400, name="West Loop")
    m.line([(0, 0), (0, -300)], "residential", name="East Street", maxspeed="25 mph")
    m.square(0, -700, 400, name="East Loop")
    m.build(conn)
    ids = group_ids(conn)
    sections.exclude_section(conn, ids["East Loop"], "closed")

    result = route_search.find_routes(conn, request(START, 1800, 2800))

    assert result["candidates"]
    for c in result["candidates"]:
        assert "East Loop" not in {w["name"] for w in c["ways"]}
    with pytest.raises(ValueError, match="excluded"):
        route_search.find_routes(conn, request(START, 1800, 2800, sections=[ids["East Loop"]]))


def test_naming_sections_restricts_the_search_to_them(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="West Street", maxspeed="25 mph")
    m.square(0, 300, 400, name="West Loop")
    m.line([(0, 0), (0, -300)], "residential", name="East Street", maxspeed="25 mph")
    m.square(0, -700, 400, name="East Loop")
    m.build(conn)
    ids = group_ids(conn)

    result = route_search.find_routes(conn, request(START, 1800, 2800, sections=[ids["East Loop"]]))

    assert result["candidates"]
    for c in result["candidates"]:
        assert c["built_from"] == "sections"
        assert used(c) == {"East Loop"}
        assert "West Loop" not in {w["name"] for w in c["ways"]}
    with pytest.raises(sections.SectionNotFoundError):
        route_search.find_routes(conn, request(START, 1800, 2800, sections=[999999]))


def test_an_unreachable_named_section_gets_a_warning_and_no_walk(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.square(0, 300, 400, name="Near Loop")
    m.square(9000, 0, 400, name="Far Loop")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 1800, 2800, sections=[group_ids(conn)["Far Loop"]]))

    assert result["candidates"] == [] and result["warnings"]


def test_the_walk_fills_in_when_the_area_has_no_sections(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 100, name="Tiny Loop")  # under the minimum section length
    m.build(conn)

    result = route_search.find_routes(conn, request(pt(0, 50), 300, 500, shape="loop"))

    assert result["candidates"]
    assert {c["built_from"] for c in result["candidates"]} == {"walk"}
    assert all(c["sections_used"] == [] for c in result["candidates"])


def test_a_flat_walk_for_a_hilly_purpose_is_a_ranked_near_miss_on_gain(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 100, name="Tiny Loop")
    m.build(conn)

    result = route_search.find_routes(conn, request(pt(0, 50), 300, 500, shape="loop", purpose="hilly"))

    assert result["near_miss"]
    for c in result["candidates"]:
        assert c["built_from"] == "walk"
        assert {miss["constraint"] for miss in c["missed_constraints"]} == {"gain_range"}
        assert c["ranking"]["gain_fit"] < 1.0 and c["ranking"]["score"] is not None


def test_the_search_budget_env_var_replaces_the_default_budget(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(route_search.BUDGET_ENV, "-1")
    req = request(START, 2700, 3300)
    del req["time_budget_s"]

    with pytest.raises(ValueError, match="time_budget_s"):
        route_search.find_routes(conn, req)


def test_the_assembly_runs_inside_the_search_slot_and_stops_at_the_deadline(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    ring_with_street().build(conn)
    held: list[bool] = []
    real = assemble.Assembly.walks

    def watching(self, deadline):
        held.append(route_search._search_lock.locked())
        yield from real(self, deadline)

    monkeypatch.setattr(assemble.Assembly, "walks", watching)
    ticks = itertools.count()
    monkeypatch.setattr(assemble.time, "monotonic", lambda: 10.0 * next(ticks))

    result = route_search.find_routes(conn, request(START, 2700, 3300, n=10, time_budget_s=3.0))

    assert held == [True]
    assert result["budget_exceeded"] is True
    assert result["candidates"] == []


def three_regions() -> Map:
    """Three 3.2 km loops of one trail system, each touching the next at a corner."""
    m = Map()
    for k in range(3):
        m.square(k * 800, k * 800, 800, name=f"Loop {k}")
    return m


def blocks_used(candidate: route_search.RouteCandidateOut) -> set[int]:
    return {u["block_id"] for u in candidate["sections_used"]}


def test_a_route_chains_regions_of_a_trail_system_none_of_which_fills_the_distance(conn: sqlite3.Connection):
    three_regions().build(conn)

    two = route_search.find_routes(conn, request(START, 5800, 7000, shape="loop"))
    three = route_search.find_routes(conn, request(START, 8800, 10400, shape="loop"))

    assert any(len(blocks_used(c)) == 2 for c in two["candidates"])
    assert any(len(blocks_used(c)) == 3 for c in three["candidates"])
    for result, (low, high) in ((two, (5800, 7000)), (three, (8800, 10400))):
        for c in result["candidates"]:
            assert c["built_from"] == "sections" and c["shape"] == "loop" and low <= c["distance_m"] <= high
            assert not c["missed_constraints"]


def test_a_candidate_reports_the_terms_of_its_score_and_the_sections_it_ran(conn: sqlite3.Connection):
    three_regions().build(conn)

    result = route_search.find_routes(conn, request(START, 5800, 7000, shape="loop"))

    chained = next(c for c in result["candidates"] if len(blocks_used(c)) == 2)
    ranking = chained["ranking"]
    assert ranking["distance_fit"] == pytest.approx(1.0)
    assert ranking["gain_fit"] == 1.0  # no purpose
    miles = chained["distance_m"] / MILE
    assert ranking["value_per_mile"] == pytest.approx(ranking["quality_mi"] / miles)
    assert ranking["repeats_effect"] == pytest.approx(1.0)  # a loop of three stretches repeats none
    assert ranking["score"] == pytest.approx(
        (ranking["value_per_mile"] - ranking["connector_cost_per_mile"]) * ranking["distance_fit"] * ranking["shape"]
    )
    assert sum(u["length_m"] for u in chained["sections_used"]) == pytest.approx(chained["distance_m"], rel=0.02)


def test_a_route_run_in_either_direction_is_one_candidate(conn: sqlite3.Connection):
    ring_with_street().build(conn)

    result = route_search.find_routes(conn, request(START, 2700, 3300))

    runs = [sorted((w["way_id"], round(w["length_m"], -1)) for w in c["ways"]) for c in result["candidates"]]
    assert runs and all(runs.count(r) == 1 for r in runs)


def test_a_later_search_honors_ways_avoided_since_an_earlier_one(conn: sqlite3.Connection):
    two_trails_to_a_summit().build(conn)
    first = route_search.find_routes(conn, request(START, 10_500, 12_300))
    return_street = next(w["way_id"] for w in first["candidates"][0]["ways"] if w["name"] == "Return Street")

    again = route_search.find_routes(conn, request(START, 10_500, 12_300, avoid_way_ids=[return_street]))

    assert again["candidates"]
    for c in again["candidates"]:
        assert return_street not in {w["way_id"] for w in c["ways"]}


def test_a_candidate_is_a_near_miss_exactly_when_it_lists_a_missed_constraint(conn: sqlite3.Connection):
    ring_with_street().build(conn)

    for min_trail_m, expected in ((1500, False), (9000, True)):
        result = route_search.find_routes(conn, request(START, 2700, 3300, min_trail_m=min_trail_m))

        assert result["candidates"] and result["near_miss"] is expected
        for c in result["candidates"]:
            assert routing_mcp._candidate_summary(c)["near_miss"] is expected


# --- credit for every mile on a section, and purpose ---------------------------


def hill_and_river() -> Map:
    """A hill north of home: Alder Trail climbs 2000 m and runs on level to 3400 m,
    a ridge track crosses to Birch Trail, which comes down. A river path runs
    east below home, joined to the hill's foot by a quiet street."""
    m = Map()
    m.line([(0, 0), (0, 400)], "residential", name="Home Lane", maxspeed="25 mph")
    m.line([(0, 400), (2000, 400)], "path", name="Alder Trail")
    m.line([(2000, 400), (3400, 400)], "path", name="Alder Trail")
    m.line([(2000, 400), (2000, 1300)], "track", name="Ridge Track")
    m.line([(2000, 1300), (0, 1300)], "path", name="Birch Trail")
    m.line([(0, 0), (-500, 0)], "residential", name="Home Street", maxspeed="25 mph")
    m.line([(-500, 0), (-500, 1300)], "path", surface="asphalt", name="River Path")
    m.line([(-500, 1300), (-500, 4400)], "path", surface="asphalt", name="River Path")
    m.line([(-500, 1300), (0, 1300)], "residential", name="Foot Street", maxspeed="25 mph")
    m.waterway([(-540, 0), (-540, 4400)])
    return m


def build_hill_and_river(conn: sqlite3.Connection) -> None:
    def height(i: int, lat: float, lng: float) -> float:
        north_m = (lat - ORIGIN_LAT) / by_deg_lat(1.0)
        return 100.0 + 0.1 * min(max(north_m, 0.0), 2000.0)

    hill_and_river().build(conn, sampler=FakeSampler(height))


def climbs_hill(candidate: route_search.RouteCandidateOut) -> bool:
    return "Alder Trail" in way_names(candidate)


@pytest.mark.usefixtures("keep_similar")
def test_a_hilly_request_prefers_the_loop_over_the_hill_and_home_by_the_river_to_retracing_the_trail(conn: sqlite3.Connection):
    build_hill_and_river(conn)

    result = route_search.find_routes(conn, request(START, 7_000, 8_200, purpose="hilly"))

    top = result["candidates"][0]
    assert top["shape"] == "loop"
    assert {"Alder Trail", "Birch Trail", "River Path"} <= set(way_names(top))
    assert max(w["length_m"] for w in top["ways"] if w["name"] == "Alder Trail") <= 2100  # not run twice


def test_an_easy_request_prefers_the_flat_river_to_the_hill(conn: sqlite3.Connection):
    build_hill_and_river(conn)

    result = route_search.find_routes(conn, request(START, 7_000, 8_200, purpose="easy"))

    top = result["candidates"][0]
    assert not climbs_hill(top) and "River Path" in way_names(top)
    for c in result["candidates"]:
        if c["score"]["gain_m"] > 100:
            assert c["ranking"]["gain_fit"] < 0.5 and c["ranking"]["score"] < top["ranking"]["score"]


@pytest.mark.usefixtures("keep_similar")
def test_the_miles_a_route_runs_on_sections_it_was_not_built_around_earn_their_value(conn: sqlite3.Connection):
    build_hill_and_river(conn)
    ids = group_ids(conn)

    result = route_search.find_routes(conn, request(START, 7_000, 8_200, sections=[ids["Alder Trail trail system"]]))

    top = result["candidates"][0]
    assert top["shape"] == "loop" and "River Path" in used(top)
    out_and_back = next(c for c in result["candidates"] if c["shape"] == "out_and_back")
    assert top["ranking"]["score"] > out_and_back["ranking"]["score"]


def test_a_candidate_that_runs_none_of_a_named_section_is_excluded(conn: sqlite3.Connection):
    build_hill_and_river(conn)
    ids = group_ids(conn)

    result = route_search.find_routes(conn, request(START, 7_000, 8_200, sections=[ids["Alder Trail trail system"], ids["River Path"]]))

    assert result["candidates"]
    for c in result["candidates"]:
        assert {"Alder Trail trail system", "River Path"} <= used(c)


# --- ranking per mile and shape ----------------------------------------------


@pytest.mark.usefixtures("keep_similar")
def test_inside_the_range_a_shorter_route_of_higher_quality_per_mile_beats_a_padded_one(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 500, name="Ridge Loop")
    m.line([(500, 500), (500, 1500)], "track", name="Ridge Loop Track")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 1900, 4300, shape="any"))

    top, padded = result["candidates"][0], max(result["candidates"], key=lambda c: c["distance_m"])
    assert top["distance_m"] < 2100 and padded["distance_m"] > 3500
    assert top["ranking"]["value_per_mile"] > padded["ranking"]["value_per_mile"]


def shape_fit_of(edges: dict[int, list[tuple[float, float]]], walk: list[tuple[int, bool]]) -> float:
    """The shape term of a route that runs the given edges (polylines of
    (north, east) meters) in the order and direction of `walk`."""
    asm = assemble.Assembly.__new__(assemble.Assembly)
    asm._points = {k: [pt(*p) for p in leg] for k, leg in edges.items()}
    asm.rows = {k: {"length_m": sum(math.dist(a, b) for a, b in zip(leg, leg[1:]))} for k, leg in edges.items()}
    return asm._shape_fit(walk)


def test_a_figure_eight_scores_lower_on_shape_than_an_open_loop_of_the_same_length():
    open_loop = shape_fit_of({1: [(0, 0), (0, 1000), (1000, 1000), (1000, 0), (0, 0)]}, [(1, True)])
    eight = shape_fit_of(
        {1: [(0, 0), (500, 500), (0, 1000), (-500, 500), (0, 0)], 2: [(0, 0), (500, -500), (0, -1000), (-500, -500), (0, 0)]},
        [(1, True), (2, True)],
    )
    assert eight < open_loop - 0.1


def test_a_route_that_doubles_back_alongside_itself_scores_lower_than_one_that_does_not():
    plain = shape_fit_of({1: [(0, 0), (0, 2000), (1000, 2000), (1000, 0), (0, 0)]}, [(1, True)])
    folded = shape_fit_of({1: [(0, 0), (0, 2000), (30, 2000), (30, 200), (60, 200), (60, 2000), (90, 2000), (90, 0), (0, 0)]}, [(1, True)])
    assert folded < plain - 0.1


def test_a_deliberate_out_and_back_is_not_penalized_for_retracing_itself():
    out = {1: [(0, 0), (0, 500), (400, 1000)]}
    assert shape_fit_of(out, [(1, True), (1, False)]) == pytest.approx(assemble.SHAPE_MAX)
