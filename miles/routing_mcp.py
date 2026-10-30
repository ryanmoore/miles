"""MCP tools for route finding: trailhead search, the route-scoring preference
profile and way overrides, and candidate route search. Distances are miles
and feet, drive times minutes; the modules underneath work in meters and
seconds, and the conversion happens only here."""

import json
import os
from collections.abc import Mapping
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

from . import candidate_sets, db, drive_starts, network, route_search, search_jobs, sections, survey
from .errors import reporting
from .fitness import FT_PER_M, MILE_M
from .offload import threaded


def _start_view(start: Mapping[str, Any]) -> dict:
    """A start as the athlete reads it: its label and drive time in minutes."""
    drive_s = start.get("drive_s")
    return {
        "lat": start["lat"],
        "lng": start["lng"],
        "label": start.get("label"),
        "drive_minutes": drive_s / 60.0 if drive_s is not None else None,
        "drive_time_estimated": start.get("drive_time_estimated", False),
    }


def _trailhead_view(start: drive_starts.DriveStart) -> dict:
    return {
        **_start_view(start),
        "kind": start["kind"],
        "osm_node_id": start["osm_node_id"],
        "trail_miles_nearby": start["trail_m_nearby"] / MILE_M,
    }


def _runnability_summary(runnability: dict) -> dict:
    out = {k: v for k, v in runnability.items() if k not in ("longest_hike_m", "steep_descent_m")}
    out["longest_hike_mi"] = runnability["longest_hike_m"] / MILE_M
    out["steep_descent_mi"] = runnability["steep_descent_m"] / MILE_M
    return out


def _candidate_summary(candidate: route_search.RouteCandidateOut) -> dict:
    score = candidate["score"]
    gain_m = score.get("gain_m")
    loss_m = score.get("loss_m")
    exposure = score["road_exposure"]
    ranking = candidate.get("ranking")
    return {
        "candidate_id": candidate["candidate_id"],
        "shape": candidate["shape"],
        "built_from": candidate.get("built_from", "walk"),
        "sections_used": [
            {"section_id": u["section_id"], "block_id": u["block_id"], "name": u["name"], "miles": _round(u["length_m"] / MILE_M)}
            for u in candidate.get("sections_used", [])
        ],
        "ranking": None if ranking is None else {
            "score": _round(ranking["score"]),
            "value_per_mile": _round(ranking["value_per_mile"]),
            "connector_cost_per_mile": _round(ranking["connector_cost_per_mile"]),
            "repeats_effect": _round(ranking["repeats_effect"], 2),
            "distance_fit": _round(ranking["distance_fit"], 2),
            "gain_fit": _round(ranking["gain_fit"], 2),
            "shape": _round(ranking["shape"], 2),
            "section_quality_miles": _round(ranking["quality_mi"]),
            "access_cost": _round(ranking["access_cost"]),
        },
        "stops": [
            {"label": st["label"], "wanted_mi": st["wanted_mi"], "at_mi": _round(st["at_mi"])}
            for st in candidate.get("stops", [])
        ],
        "start": _start_view(candidate["start"]),
        "distance_mi": candidate["distance_m"] / MILE_M,
        "trail_mi": score["trail_distance_m"] / MILE_M,
        "singletrack_mi": score.get("singletrack_distance_m", 0.0) / MILE_M,
        "dirt_road_or_track_mi": score.get("track_distance_m", 0.0) / MILE_M,
        "unpaved_road_mi": score["unpaved_road_distance_m"] / MILE_M,
        "paved_mi": score["paved_distance_m"] / MILE_M,
        "connector_mi": score["connector_distance_m"] / MILE_M,
        "gain_ft": gain_m * FT_PER_M if gain_m is not None else None,
        "loss_ft": loss_m * FT_PER_M if loss_m is not None else None,
        "has_elevation": score["has_elevation"],
        "elevation_coverage_pct": score["elevation_coverage_pct"],
        "runnability": _runnability_summary(score["runnability"]),
        "road_exposure": {
            "max_continuous_uncomfortable_mi": exposure["max_continuous_uncomfortable_m"] / MILE_M,
            "totals_mi": {k: v / MILE_M for k, v in exposure["totals_m"].items()},
        },
        "near_miss": bool(candidate["missed_constraints"]),
        "missed_constraints": candidate["missed_constraints"],
    }


def _candidate_detail(candidate: route_search.RouteCandidateOut) -> dict:
    """The stored candidate with its ways reported in miles."""
    ways = [
        {
            "way_id": w["way_id"],
            "name": w["name"],
            "surface_class": w["surface_class"],
            "length_mi": w["length_m"] / MILE_M,
        }
        for w in candidate["ways"]
    ]
    return {**candidate, "ways": ways}


def _search_reply(view: dict) -> dict:
    """A search_jobs view as the tools answer it: candidates summarized, the
    finished search's result fields alongside."""
    out: dict = {
        "search_id": view["search_id"],
        "status": view["status"],
        "purpose": view["purpose"],
        "elapsed_s": _round(view["elapsed_s"], 1),
        "qualifying": view["qualifying"],
        "candidates": [_candidate_summary(c) for c in view["candidates"]],
    }
    result = view["result"]
    if result is not None:
        out.update(
            near_miss=result["near_miss"], budget_exceeded=result["budget_exceeded"], seed=result["seed"]
        )
        if result["warnings"]:
            out["warnings"] = result["warnings"]
    if view["error"] is not None:
        out["error"] = view["error"]
    return out


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _section_row_view(row: survey.SurveyRow) -> dict:
    """A survey row in miles and feet, with the scenery trimmed to what a
    conversation needs."""
    scenery = row["scenery"]
    miles = row["union_m"] / MILE_M
    runnability = row["runnability"]
    access = row["access"]
    terms = row["terms"]
    pace = runnability["est_pace_s_per_mi"]
    view: dict = {
        "section_id": row["section_id"],
        "rank": row["rank"],
        "excluded": row["excluded"],
        "kind": row["kind"],
        "kinds": row["kinds"],
        "name": row["name"],
        "level": row["level"],
        "length_mi": _round(row["length_m"] / MILE_M),
        "surface_pct": {k: round(v) for k, v in row["surface_pct"].items()},
        "junctions_per_mi": _round(row["junctions"] / max(miles, 0.25), 1),
        "busy_road_crossings": row["crossings"],
        "scenery": {
            "tags": scenery["tags"],
            "water": scenery["water_names"],
            "parks": scenery["area_names"],
            "routes": scenery["route_names"],
            "summits": scenery["summits"],
            "viewpoints": scenery["viewpoints"],
        },
        "runnability": {
            "run_pct": _round(runnability["run_pct"], 0),
            "grind_pct": _round(runnability["grind_pct"], 0),
            "hike_pct": _round(runnability["hike_pct"], 0),
            "est_pace_min_per_mi": _round(pace / 60.0 if pace is not None else None, 1),
        },
        "gain_ft_per_mi": _round(row["gain_m_per_mile"] * FT_PER_M if row["gain_m_per_mile"] is not None else None, 0),
        "familiarity": row["familiarity"],
        "access": {
            "approach_mi": _round(access["approach_m"] / MILE_M if access["approach_m"] is not None else None),
            "uncomfortable_mi": _round(access["uncomfortable_m"] / MILE_M),
            "steepest_pitch_pct": _round(access["steepest_pitch_pct"], 1),
        },
        "adjustment": row["adjustment"],
        "terms": {
            "usable_mi": _round(terms["usable_m"] / MILE_M),
            "continuity": _round(terms["continuity"], 3),
            "scenery_bonus": _round(terms["scenery_bonus"], 3),
            "runnability": _round(terms["runnability"], 3),
            "surface": _round(terms["surface"], 3),
            "quality": _round(terms["quality"]),
            "adjustment": terms["adjustment"],
            "access_cost": _round(terms["access_cost"]),
        },
        "score": _round(row["score"]),
    }
    if row["group_id"] is not None:
        view["group_id"] = row["group_id"]
    if row["stretches"]:
        view["stretches"] = {"total": row["stretches_total"], "listed": [
            {"section_id": st["section_id"], "role": st["role"], "length_mi": _round(st["length_m"] / MILE_M)}
            for st in row["stretches"]]}
    if row["role"]:
        view["role"] = row["role"]
    if row["sides"]:
        view["sides"] = row["sides"]
    if row["notes"]:
        view["notes"] = row["notes"]
    if row["other_names"]:
        view["also_called"] = row["other_names"]
    if row["parallel_m"] > 0:
        view["parallel_paths"] = {
            "parallel_mi": _round(row["parallel_m"] / MILE_M),
            "union_mi": _round(row["union_m"] / MILE_M),
            "note": "parallel paths (such as both banks of a waterway) lie within the section; length_mi is "
                    "one way along it, union_mi counts every path",
        }
    if row["fragment"]:
        view["fragment"] = True
    exclusion = row.get("exclusion")
    if exclusion:
        view["exclusion"] = exclusion
    return view


def _survey_view(result: dict) -> dict:
    return {
        **{k: v for k, v in result.items() if k not in ("distance_m", "run_distance_m")},
        "distance_mi": _round(result["distance_m"] / MILE_M if result["distance_m"] is not None else None),
        "run_distance_mi": _round(result["run_distance_m"] / MILE_M),
        "sections": [_section_row_view(r) for r in result["sections"]],
        "score_formula": "score = quality x adjustment x gain_fit - access_cost (quality-miles); see terms on each section",
    }


def register_routing_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @threaded
    @reporting(ValueError)
    def find_trailheads(area_id: int, lat: float, lng: float, max_drive_minutes: float, k: int = 8) -> str:
        """
        Trailheads and trail-adjacent parking in an imported area within
        max_drive_minutes (at most 90) of (lat, lng). Returns the point itself,
        always first as a zero-drive-time start, followed by up to k starts
        (k + 1 results at most), best first by drive time and nearby trail.

        Each result has lat/lng, label, drive_minutes, kind
        ("origin"/"trailhead"/"parking"), the OSM id (null for the origin;
        negated way id for an outline), trail_miles_nearby, and
        drive_time_estimated (true when routing was unavailable and the time
        is a straight-line estimate).
        """
        with db.connection() as conn:
            starts = drive_starts.drive_starts(conn, area_id, lat, lng, max_drive_minutes, k=k)
        return json.dumps([_trailhead_view(s) for s in starts])

    @mcp.tool()
    def get_route_preferences() -> str:
        """
        The route-scoring preference profile: road-comfort thresholds, maximum
        continuous uncomfortable-road distance. Defaults apply until
        set_route_preferences changes them.
        """
        with db.connection() as conn:
            return json.dumps(network.get_route_preferences(conn))

    @mcp.tool()
    @reporting(ValueError)
    def set_route_preferences(patch: dict[str, Any]) -> str:
        """
        Update the preference profile. patch is a JSON object with any subset
        of get_route_preferences' top-level keys; dict-valued keys are merged
        into the existing value. Returns the full profile.
        """
        with db.connection() as conn:
            return json.dumps(network.set_route_preferences(conn, patch))

    @mcp.tool()
    @reporting(ValueError)
    def set_way_override(way_id: int, verdict: str, note: str | None = None) -> str:
        """
        Mark an OSM way "comfortable" or "avoid" regardless of its tags; survives
        network rebuilds. Way ids come from the ways list of
        get_route_candidate.
        """
        with db.connection() as conn:
            return json.dumps(network.set_way_override(conn, way_id, verdict, note))

    @mcp.tool()
    @threaded
    @reporting(ValueError, sections.SectionNotFoundError)
    def find_routes(
        area_id: int,
        distance_mi_min: float,
        distance_mi_max: float,
        start_lat: float | None = None,
        start_lng: float | None = None,
        max_drive_minutes: float | None = None,
        drive_k: int = 8,
        shape: str = "any",
        min_trail_mi: float | None = None,
        min_runnable_pct: float | None = None,
        gain_ft_min: float | None = None,
        gain_ft_max: float | None = None,
        avoid_way_ids: list[int] | None = None,
        include_way_ids: list[int] | None = None,
        n: int = 5,
        starts: list[route_search.Start] | None = None,
        seed: int | None = None,
        sections: list[int] | None = None,
        repeat_value: float = route_search.DEFAULT_REPEAT_VALUE,
        stops: list[route_search.Stop] | None = None,
        surface_preference: str | None = None,
        unfamiliar_only: bool = False,
        purpose: str | None = None,
    ) -> str:
        """
        Find candidate running routes over an imported area's network graph
        (import_area first if the area isn't ready). Give the start(s) one way:
        - start_lat/start_lng alone: a single start.
        - start_lat/start_lng plus max_drive_minutes (at most 90): fans out over
          the trailheads and parking find_trailheads returns (best drive_k,
          default 8) plus the point itself. Drive times are straight-line
          estimates, flagged drive_time_estimated=true, when OSRM is down; tell
          the athlete and re-run later.
        - starts: a list of {"lat", "lng", "label"?, "drive_s"?} objects.
          drive_s is drive time in seconds (find_trailheads reports
          drive_minutes, so multiply by 60).

        distance_mi_min and distance_mi_max (required) bound the length.
        shape is "loop", "out_and_back", "lollipop", or "any".
        avoid_way_ids/include_way_ids are lists of OSM way ids, which come
        from the ways list of get_route_candidate.

        Hard constraints are never violated by a returned candidate: avoided
        ways (avoid_way_ids, ways marked "avoid" with set_way_override, and the
        ways of sections the athlete excluded with exclude_section) and the
        road-exposure limit in the route preferences. Everything else
        is soft and reported in missed_constraints when a candidate misses it:
        the distance range, min_trail_mi, min_runnable_pct (run plus grind,
        i.e. not hiking), the gain_ft_min/gain_ft_max range, and
        include_way_ids (so a candidate can still lack an included way).

        Routes are built around sections (survey_area): with sections=[ids]
        (group or stretch ids, from survey_area or get_section), around
        exactly those; without, around the survey's best sections for the
        start and distance, and the randomized walk fills in only when those
        give no route that meets the request (built_from says which:
        "sections" or "walk"). A section-built candidate is one search over
        the sections' stretches: a walk that runs stretches, joins them with
        connectors (the cheapest comfortable way between two points) and ends at
        the start; shape only filters what the finished walk looks like. Naming
        sections never falls back to the walk: if none can be reached, warnings
        says so. sections_used lists what each candidate runs: section_id,
        block_id, name and the miles on it (a mile run twice counts twice).

        Every mile a route runs on any section earns that section's adjusted
        quality, named or not, on a stretch the search built around or one a
        connector crosses; only miles on no section are charged as connector
        cost. sections=[ids] therefore means "must include": a candidate
        qualifies only if it runs at least 400 m of each named section (or a
        tenth of it, if shorter), and is scored as a whole.

        Candidates rank by quality per mile, not total, so inside the
        distance range a longer route does not win by length alone:
        ranking.score = (value_per_mile - connector_cost_per_mile) x
        distance_fit x gain_fit x shape. value_per_mile is the adjusted survey
        quality of the miles run on sections, per mile of route (section_quality_miles
        is the total); connector_cost_per_mile the weighted cost of the miles
        on no section, per mile; distance_fit is 1 inside the distance range
        and falls outside it; gain_fit is 1 inside the purpose's climb band
        (below) and falls outside it; shape (0.6 to 1.15) rewards a route that
        reaches out from the start and penalizes one that runs close beside
        itself (a figure eight, a wiggle, a filler spur). A stretch run again
        earns repeat_value (default 0.5, 0 to 1) of its quality the second time
        and its square after that, shown as repeats_effect (the share of the
        quality left after repeats, already in value_per_mile): 0 when the
        athlete wants no repeats, 0.8 to 1 when they do not mind retracing a
        favorite stretch. The returned candidates are meant to differ: one
        running over the same ground as a higher-ranked one for more than 60%
        of its length is not returned, so fewer than n can come back.
        surface_preference ("singletrack", "track", "paved", "any") and
        unfamiliar_only are survey_area's, used to choose the sections when
        none are named.

        purpose is what the run is for; set it whenever the athlete's words
        say, and tell them which you used. "easy"/"recovery" (up to about 40
        ft of climb per mile; hiking and steep grind cost the most) for an easy
        run; "workout"/"tempo"/"intervals" (up to 30 ft per mile, continuous
        sections weighted up) for a quality session; "long" (no climb
        preference, scenery weighted up) for a long run or a trail day;
        "hilly" (at least 120 ft per mile, grind and short hikes cost little)
        for hills, climbing, or vert. Omit it when none applies. The purpose
        sets the climb band a candidate is scored against (gain_ft_min and
        gain_ft_max, when given, replace the band), how much grind and hiking
        count for, how much junctions and crossings count against a section,
        and how much scenery counts for; it also steers which sections are
        chosen and how the search values them. It does not apply to the
        random walk that fills in when no section serves the request.

        stops are points the route must pass: {"lat", "lng", "label"?, "at_mi"?}.
        With at_mi the route passes the stop within a mile of that distance
        along the route (a refill at home around mile 8, a lot at the halfway
        point); without it, anywhere. A stop snaps to the nearest path within
        500 m, so its position is the network's; each candidate's stops lists
        wanted_mi and the at_mi it was actually passed at. A route with stops
        is always built from sections, never the walk.

        The search runs in the background and this returns at once with a
        search_id and status "running" (or "queued" behind another search).
        Follow it with get_search(search_id, wait_s=40) until its status is
        "done"; cancel_search stops it. A search is a safety-capped
        computation, not a ten-second one: the server's budget
        (MILES_SEARCH_BUDGET_S, default 10 seconds) is how long it may run,
        and on slow hardware that is minutes. Argument errors (a bad range,
        an area that is not ready) are returned here; the rest show as an
        "error" status. Searches run one at a time per server.

        Up to n candidates (default 5) come back from get_search: start,
        distance, trail/road mix, runnability, road exposure, and a
        candidate_id for get_route_candidate/save_route_candidate. When
        nothing meets every soft constraint, the closest few come back, each
        with near_miss=true and what it misses in missed_constraints (the
        result's near_miss is true then too). budget_exceeded=true means the
        time budget ran out before n candidates qualified, so the result may
        be thin; narrowing the request or a different seed can help. The same
        seed, request, and network reproduce the same candidates; the seed
        used is always returned. In each candidate's runnability,
        longest_hike_mi and steep_descent_mi are distances along the route
        (miles of continuous hiking, miles steeper than the athlete's descent
        threshold), not vertical feet.
        """
        with db.connection() as conn:
            start_list = route_search.resolve_starts(
                conn,
                area_id,
                starts=starts,
                origin=(start_lat, start_lng) if start_lat is not None and start_lng is not None else None,
                max_drive_minutes=max_drive_minutes,
                drive_k=drive_k,
            )
            if shape not in ("loop", "out_and_back", "lollipop", "any"):
                raise ValueError(f"invalid shape: {shape!r}")

            request: route_search.RouteSearchRequest = {
                "area_id": area_id,
                "starts": start_list,
                "distance_m_min": distance_mi_min * MILE_M,
                "distance_m_max": distance_mi_max * MILE_M,
                "shape": cast(route_search.Shape, shape),
                "n": n,
                "min_trail_m": None if min_trail_mi is None else min_trail_mi * MILE_M,
                "min_runnable_pct": min_runnable_pct,
                "gain_m_min": None if gain_ft_min is None else gain_ft_min / FT_PER_M,
                "gain_m_max": None if gain_ft_max is None else gain_ft_max / FT_PER_M,
                "avoid_way_ids": avoid_way_ids,
                "include_way_ids": include_way_ids,
                "seed": seed,
                "sections": sections,
                "repeat_value": repeat_value,
                "stops": stops,
                "surface_preference": surface_preference,
                "unfamiliar_only": unfamiliar_only,
                "purpose": purpose,
            }
            return json.dumps(_search_reply(search_jobs.start(conn, request)))

    @mcp.tool()
    @threaded
    @reporting(search_jobs.UnknownSearchError)
    def get_search(search_id: str, wait_s: float = 0) -> str:
        """
        The state of a find_routes search: status ("queued", "running",
        "done", "cancelled" or "error", with error saying why), elapsed_s and
        qualifying (candidates that meet every soft constraint so far).
        candidates is the best found so far while it runs, then the final
        list, in find_routes' candidate format; results from a running search
        can still change. Once finished, the reply also has near_miss,
        budget_exceeded, seed, and warnings, as find_routes' results used to.

        wait_s (at most 45) holds the call until the search finishes or that
        many seconds pass, so a long search is followed with repeated calls,
        each wait_s=40. A search is lost if the server restarts, or an hour
        after it finishes; the answer then says so.
        """
        return json.dumps(_search_reply(search_jobs.get(search_id, wait_s)))

    @mcp.tool()
    @threaded
    @reporting(search_jobs.UnknownSearchError)
    def cancel_search(search_id: str) -> str:
        """
        Stop a find_routes search and return get_search's reply: status
        "cancelled" with the candidates found so far, which can be shared and
        saved like any other. Use it when the athlete changes the request or
        the search runs longer than they will wait.
        """
        return json.dumps(_search_reply(search_jobs.cancel(search_id)))

    @mcp.tool()
    @reporting(route_search.CandidateNotFoundError)
    def get_route_candidate(candidate_id: str) -> str:
        """
        Full breakdown and geometry for a find_routes candidate, plus its
        ways in travel order: consecutive stretches of one OSM way, each with
        way_id, name (may be null), surface_class, and length_mi. This is the
        only place way ids come from; use them for find_routes'
        avoid_way_ids/include_way_ids and for set_way_override. Candidates
        expire a day after their search.
        """
        with db.connection() as conn:
            return json.dumps(_candidate_detail(route_search.get_route_candidate(conn, candidate_id)))

    @mcp.tool()
    @reporting(route_search.CandidateNotFoundError)
    def save_route_candidate(candidate_id: str, name: str) -> str:
        """
        Save a candidate as a route (source "generated") with a GPX built from
        its geometry; it then works like an uploaded route.
        """
        with db.connection() as conn:
            return json.dumps(route_search.save_route_candidate(conn, candidate_id, name))

    @mcp.tool()
    @reporting(route_search.CandidateNotFoundError, ValueError)
    def share_candidates(candidate_ids: list[str], title: str, notes: dict[str, str] | None = None) -> str:
        """
        Publish find_routes candidates as a page the athlete opens on their
        phone: one card each with a map, elevation profile, stats, and a
        Save as route button. Call it after find_routes with the 2-6
        candidates worth considering, in the order to show them, and a
        one-line note each in `notes` (keyed by candidate_id; why this one,
        what sets it apart). Then give the athlete the link.

        Returns set_id, url, and expires_at. The set and its candidates are
        kept for a week. url is absolute when the server has MILES_PUBLIC_URL
        set; otherwise it is a path (/candidates.html?set=...) to open on the
        miles web app's own address.
        """
        with db.connection() as conn:
            set_id, expires_at = candidate_sets.create_set(conn, candidate_ids, title, notes)
        base = os.environ.get("MILES_PUBLIC_URL", "").rstrip("/")
        return json.dumps({"set_id": set_id, "url": f"{base}/candidates.html?set={set_id}", "expires_at": expires_at})

    @mcp.tool()
    @threaded
    @reporting(ValueError, sections.SectionNotFoundError)
    def survey_area(
        area_id: int,
        start_lat: float,
        start_lng: float,
        distance_mi: float | None = None,
        kinds: list[str] | None = None,
        unfamiliar_only: bool = False,
        adjustments: dict[str, float] | None = None,
        n: int = 10,
        surface_preference: str | None = None,
        include_fragments: bool = False,
        detail: str = "overview",
        within: int | None = None,
        purpose: str | None = None,
    ) -> str:
        """
        Survey an imported area for stretches worth running before building a
        route: the best sections around a start, ranked, each with its traits,
        what it costs to reach, and how often the athlete has run it. Open a
        route conversation with this, then build routes around what the
        athlete picks.

        A section is a part of the map run as a unit. The survey has two
        levels. detail="overview" (the default) ranks groups: one group is
        everything that covers one stretch of ground (a riverside path that is
        also a trail system and a named route, both banks, the loops inside
        it), listed once with its single-path length_mi, the best approach to
        any of its stretches, and its stretches under "stretches" (section_id,
        role, length_mi). A group is cut into stretches (at bridges, busy
        roads, trailheads and other junctions; a network of trails is divided
        into regions of a few miles, named for their main trails): the parts
        of it a route can be built around. A stretch's notes list the short
        spurs it includes, which count toward its length. detail="detail" ranks the stretches instead, and
        within=<group section_id> (which implies detail) lists just that
        group's stretches in order, each with a role such as "between X and
        Y", its sides, approach and familiarity: use it when the athlete picks
        an area or asks which part. Every id works with get_section,
        adjust_section, exclude_section and clear_section_adjustment. kinds
        (any subset to filter by): trail_system (a connected cluster of natural-surface
        trails), water_path (paths along a river, stream, canal, or lake),
        named_route (a mapped walking, hiking, cycling, or running route),
        park_paths (paths inside one park or protected area), quiet_road (a
        run of comfortable road with few junctions). A stretch that fits more
        than one kind (a named path along a river) is one section: kind is its
        main kind, kinds lists all of them, and the kinds filter matches any.
        Section ids last until the area's network is next rebuilt; survey
        again after that.

        distance_mi (optional) is the intended run length; it bounds how far
        an approach may be (a quarter of it, each way) and how much of a
        section counts. unfamiliar_only drops sections the athlete's own
        activity tracks have covered; familiarity needs their streams
        matched, which fills in over time (see familiarity in the result).
        n is how many ranked sections to return (default 10, at most 25).

        surface_preference is "singletrack", "track", "paved", or "any" (the
        default). Sections report singletrack (footpath-width natural-surface
        trail) and track (road-width dirt road, canal or levee bank, fire
        road) separately in surface_pct. By default a track counts 0.8 of
        singletrack toward quality; pass "singletrack" or "track" when the
        athlete asks for one ("technical trails", "a flat dirt road") or
        "paved" for the opposite. include_fragments=True also ranks the stubs
        and connectors left out by default (under 1.5 miles or 15% of the
        run distance, whichever is more; judged per group in an overview, per
        stretch in detail); fragments_hidden counts them.

        purpose ("easy", "workout", "long", "hilly") is find_routes': it weighs
        a section's runnability, continuity and scenery for that kind of run
        and multiplies its score by gain_fit, which falls as its climb per
        mile (gain_ft_per_mi) leaves the purpose's band. Use the same purpose
        for the survey and the search.

        adjustments is {section_id: factor} for this request only, nothing
        stored: use it for what you know of a place that the map lacks, with
        factors between 0.5 and 2.0. Say where the knowledge came from; offer
        to store it with adjust_section (source "research", with a link) only
        if the athlete agrees. Stored adjustments appear on each section with
        their source and note.

        Each section reports: length_mi (one way along it: parallel_paths says
        when it also holds paths beside the main one, such as the other bank,
        and gives the union), surface_pct, junctions_per_mi,
        busy_road_crossings, scenery (tags, water, parks, routes, summits,
        viewpoints), runnability for this athlete (run/grind/hike percent and
        an estimated pace from their terrain model), gain_ft_per_mi,
        familiarity (runs that covered it, last run, new_to_you; a group is
        matched over its whole extent), access (approach_mi from the start to
        the nearest of its stretches, uncomfortable_mi of it, steepest pitch),
        the adjustment (a stretch's includes its group's), every term of the score, and the score. Sections whose
        approach is too long or has more continuous uncomfortable road than
        the route preferences allow are not ranked (ruled_out lists a few).
        Sections the athlete excluded are listed last with excluded=true.

        To build routes around what the athlete picks, pass the section ids
        to find_routes as sections.
        """
        with db.connection() as conn:
            result = survey.survey(
                conn,
                area_id,
                start_lat,
                start_lng,
                distance_m=None if distance_mi is None else distance_mi * MILE_M,
                kinds=kinds,
                unfamiliar_only=unfamiliar_only,
                adjustments=adjustments,
                n=n,
                surface_preference=surface_preference,
                include_fragments=include_fragments,
                detail=detail,
                within=within,
                purpose=purpose,
            )
        return json.dumps(_survey_view(result))

    @mcp.tool()
    @threaded
    @reporting(ValueError, sections.SectionNotFoundError)
    def get_section(section_id: int) -> str:
        """
        One section from survey_area in detail, a group or stretch: its
        ways (OSM way id, name, length_mi, longest first) and a simplified
        geometry as a list of [lat, lng] polylines for drawing or steering. For
        a group it also lists its stretches in order (section_id, role,
        length_mi) so one can be picked. The way ids work as
        find_routes' include_way_ids (soft: a candidate can still miss them)
        and as set_way_override targets; pass avoid_way_ids only for ways the
        athlete wants kept out of a route.
        """
        with db.connection() as conn:
            section = sections.get_section(conn, section_id)
            geometry = sections.section_geometry(conn, section)
            stretches = sections.stretches_of(conn, section_id) if section.level == sections.GROUP else []
        return json.dumps(
            {
                "section_id": section.section_id,
                "area_id": section.area_id,
                "kind": section.kind,
                "kinds": section.kinds,
                "level": section.level,
                "group_id": section.group_id,
                "name": section.name,
                "length_mi": _round(section.single_path_m / MILE_M),
                "stretches": [
                    {"section_id": st.section_id, "role": st.data.get("role") or st.name, "length_mi": _round(st.single_path_m / MILE_M)}
                    for st in stretches
                ],
                "ways": [
                    {"way_id": w[0], "name": w[2], "length_mi": _round(w[1] / MILE_M)} for w in section.data["ways"]
                ],
                "geometry": geometry,
            }
        )

    @mcp.tool()
    @reporting(ValueError, sections.SectionNotFoundError)
    def adjust_section(
        section_id: int, factor: float, note: str, source: str = "athlete", url: str | None = None
    ) -> str:
        """
        Store a judgment about a section that the map cannot show: a factor on
        its measured quality, kept across rebuilds. Only store one when the
        athlete says so, or confirms one you proposed; a guess belongs in
        survey_area's per-request adjustments. section_id is a group or a
        stretch: a group's factor applies to all its stretches, a stretch's to
        that stretch alone (and multiplies with its group's); store it at the
        level the athlete means ("the whole riverside path" is a group, "the
        part past the bridge" a stretch). factor is clamped to 0.5-2.0
        (1.0 is no change; 1.5 is notably better than it measures, 0.7
        somewhat worse). source is "athlete" for their own verdict, or
        "research" for what you or a search found, which needs a url to the
        source. note says why, in a sentence. Replaces any adjustment the
        section already has. Returns the stored adjustment.
        """
        with db.connection() as conn:
            return json.dumps(sections.adjust_section(conn, section_id, factor, note, source=source, url=url))

    @mcp.tool()
    @reporting(ValueError, sections.SectionNotFoundError)
    def exclude_section(section_id: int, note: str) -> str:
        """
        Rule a section out of every route search until undone: its ways leave
        the graph, as ways marked "avoid" do. ONLY on the athlete's explicit
        instruction to rule a place out; never on your own judgment or
        research (use a low adjustment for those). A stretch's ways leave the
        graph; a group's exclusion removes all its stretches' ways. The section
        stays listed in survey_area, marked excluded. note says why.
        """
        with db.connection() as conn:
            return json.dumps(sections.exclude_section(conn, section_id, note))

    @mcp.tool()
    @reporting(ValueError, sections.SectionNotFoundError)
    def clear_section_adjustment(section_id: int, exclusion: bool = False) -> str:
        """
        Remove a section's stored adjustment. exclusion=True removes its
        exclusion as well, which only the athlete's explicit instruction may
        do. Per-request adjustments in survey_area are never stored, so there
        is nothing to clear for them.
        """
        with db.connection() as conn:
            return json.dumps(sections.clear_section_adjustment(conn, section_id, exclusion=exclusion))
