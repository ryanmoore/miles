"""One search over a section graph (adr/0004-section-first-route-search.md).

A route is a walk from the start that stitches stretches of sections together
with connectors, and ends back at the start inside the distance range after
passing every stop.

The section graph has two kinds of edge:

- running: an edge of a chosen stretch (cut into pieces of about PIECE_M so a
  walk can turn anywhere), walked in either direction where one-way tags allow,
  worth the stretch's adjusted quality per mile (survey.density x the section's
  factor) times the miles run. A piece run again earns repeat_value of that, and
  a piece run a third time or more repeat_value squared.
- connecting: the preference-weighted shortest path between two hubs (the
  start, the stops, and the entry and end nodes of the stretches), worth nothing
  and charged CONNECTOR_CHARGE_PER_WEIGHTED_MI. Paths are computed in the
  direction of travel. The start and the stops are sinks that every hub, and
  every junction of a stretch, can connect to; each sink also gets a way in that
  avoids the corridor its way out uses, so a loop can come home by another road.

A bounded beam search walks the graph in layers of BUCKET_M of distance, keeping
BEAM_WIDTH walks per layer, ranked by score so far plus an optimistic bound on
what the miles left can earn. Walks ending at the start inside the range, with
every stop passed inside its window, are collected. The search runs ROUNDS times,
each time discounting the stretches of the best walk so far, so the walks that come
back are not all variations of one; an open request also spends rounds on its best
groups (ANCHORS).

Every mile on a stored stretch earns that stretch's adjusted quality, whether the
walk runs it as a piece or crosses it inside a connector; only miles on no
stretch are charged. Repeats are counted per piece, or per edge off the chosen
stretches, whichever way the mile was run.

    score = (quality - access_cost) per mile x fit x gain_fit x shape

    quality     = sum over the miles run on stretches of density x meters,
                  repeats discounted as above
    fit         = 1 inside the requested distance range; outside it, falling
                  by FIT_SLOPE of the miss as a share of the target, to 0
    gain_fit    = 1 inside the purpose's climb band (survey.gain_fit); always 1
                  with no purpose
    access_cost = the weighted miles of connector on no stretch at
                  CONNECTOR_CHARGE_PER_WEIGHTED_MI
    shape       = SHAPE_MIN..SHAPE_MAX: how far the route reaches from the start
                  as a share of its length (a route that stays near home folds
                  back on itself), less the share of its length that runs within
                  PROXIMITY_M of a part of itself at least PROXIMITY_APART_M away
                  along the route. Miles run twice are left out of that share:
                  repeat_value has already charged them.

A route is ranked per mile because its value otherwise grows with its length,
and inside the range the longest padded walk would always win. The beam's
bound on partial walks stays in total value, which is what the miles left can
still earn; finished walks are compared per mile.
"""

import itertools
import json
import math
import sqlite3
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Hashable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple, TypeVar, cast

from . import familiarity, gpx_parse, network, route_search, routes_spatial, sections, survey, terrain
from .fitness import FT_PER_M
from .route_search import GraphEdge, NodeInfo, SearchGraph, Start, Stop, Traversal

MILE_M = survey.MILE_M
_T = TypeVar("_T")

# Sections taken from the survey when a request names none, per start.
SURVEY_N = 8
MAX_SURVEY_STARTS = 4
STEER_TRAIL = 0.5
# The search values a stretch above a gain band's top by no less than this share.
STEER_FLOOR = 0.25
# A route runs a section it was told to include when it covers MIN_INCLUDE_SHARE
# of the section or MIN_INCLUDE_M of it, whichever is less. A section a route
# covers less than MIN_INCLUDE_M of is incidental, not part of what it runs.
MIN_INCLUDE_SHARE = 0.1
MIN_INCLUDE_M = 400.0

# A connector to or from the start or a stop may be at most this share of the
# target distance long; the trees it comes from are explored to
# APPROACH_TREE_SHARE of the target in weighted cost.
APPROACH_SHARE_MAX = 0.3
APPROACH_TREE_SHARE = 0.6
# Connectors between hubs are explored to this weighted cost, and a hub keeps
# its cheapest CONNECTORS_PER_HUB.
CONNECTOR_MAX_COST_M = 1500.0
CONNECTORS_PER_HUB = 12
# A route's connectors are charged at this per weighted mile: half the survey's
# one-way access charge, since a route pays both ways of its approach.
CONNECTOR_CHARGE_PER_WEIGHTED_MI = survey.ACCESS_COST_PER_WEIGHTED_MI / 2.0
# A stretch's edges are cut into pieces of about this length.
PIECE_M = 250.0

BEAM_WIDTH = 200
# Walks are grouped in layers of this much distance; a step always moves to a later layer.
BUCKET_M = 150.0
# A walk may finish this share of the target outside the range, and is scored by fit there.
FIT_SLACK_SHARE = 0.05
FIT_SLOPE = 2.0
# A stop with a requested mile counts when the walk passes it within this many miles of it.
STOP_WINDOW_MI = 1.0
# A start is searched this many times, the stretches of the best walk so far
# worth ROUND_DISCOUNT of their value each time. Finished walks considered per
# round, distinct ones kept per round, and returned.
ROUNDS = 5
ROUND_DISCOUNT = 0.4
# An open request's best survey groups are anchors: after the first round, each
# gets a round with its stretches worth ANCHOR_BOOST times as much, so the walks
# start out spread over different parts of the map.
ANCHORS = 3
ANCHOR_BOOST = 3.0
SCANNED_COMPLETIONS = 600
MAX_COMPLETIONS = 60
MAX_WALKS = 120

# The shape term (module docstring). A plain loop reaches about a third of its
# length from the start, so a quarter counts as fully open.
SHAPE_MIN = 0.6
SHAPE_MAX = 1.15
OPEN_REACH = 0.25
PROXIMITY_M = 50.0
PROXIMITY_APART_M = 400.0

# A walk that repeats this share of its length is an out-and-back, and one whose
# last edges retrace its first by MIN_STEM_M or more, a lollipop.
OUT_AND_BACK_REUSE_SHARE = 0.5
MIN_STEM_M = 200.0

# Connector trees cached per area, graph build and avoided ways, and the most
# kept in all.
MAX_SPACES = 3
MAX_CACHED_TREES = 3000


@dataclass
class PlannedBlock:
    block: sections.Block
    # Adjusted quality per mile: survey.density x the section's factor.
    density: float
    # What the search (never the score) multiplies the block's value by, to
    # lean toward the request's gain and trail minimums.
    steer: float = 1.0
    # False for a stretch the search only credits miles on, never builds around.
    runs: bool = True


@dataclass
class BlockPlan:
    """Every stretch of the area as a block: those to build routes around run,
    the rest only earn the miles a connector crosses on them."""
    blocks: list[PlannedBlock] = field(default_factory=list)
    explicit: bool = False
    warnings: list[str] = field(default_factory=list)
    overrides: dict[int, str] = field(default_factory=dict)
    # The climb per mile (ft) the request wants, and whether a route is scored
    # against it (only when it names a purpose).
    gain_band: tuple[float | None, float | None] = (None, None)
    fit_gain: bool = False
    # For each section the request names: the blocks it covers and the least
    # a route must cover of them.
    required: list[tuple[list[int], float]] = field(default_factory=list)
    # For an open request: the blocks of each of the survey's best groups.
    anchors: list[list[int]] = field(default_factory=list)

    def keep_nodes(self, rows: Mapping[int, dict]) -> set[int]:
        """The end nodes of every edge of the blocks to build around, which the
        search graph keeps even beyond its radius."""
        nodes: set[int] = set()
        for planned in self.blocks:
            if not planned.runs:
                continue
            for eid in planned.block["edge_ids"]:
                row = rows.get(eid)
                if row is not None:
                    nodes.update((row["from_node_id"], row["to_node_id"]))
        return nodes


# ---------------------------------------------------------------------------
# Choosing blocks
# ---------------------------------------------------------------------------


def _gain_band(request: route_search.RouteSearchRequest, purpose: survey.Purpose, target_m: float) -> tuple[float | None, float | None]:
    """The climb per mile (ft) the request wants: its own gain range if it
    gives one, else its purpose's band."""
    gain_min, gain_max = request.get("gain_m_min"), request.get("gain_m_max")
    if not (gain_min or gain_max):
        return purpose.gain_ft_per_mi
    per_mile = FT_PER_M * MILE_M / target_m
    return (gain_min * per_mile if gain_min else None, gain_max * per_mile if gain_max else None)


def _steer(stretch: sections.Section, band: tuple[float | None, float | None], request: route_search.RouteSearchRequest) -> float:
    """Soft constraints only filter a finished walk, which leaves a request for
    a hilly or trail-heavy route to the walks that happen to score best. So the
    search values a stretch more the more it helps: one point for the share of
    the band's climb per mile it has, STEER_TRAIL for being trail; and less the
    further its climb is over the band's top."""
    steer = 1.0
    lo, hi = band
    elevated = stretch.data["elevated_m"]
    if elevated > 0:
        ft_per_mile = stretch.data["gain_m"] * FT_PER_M / (elevated / MILE_M)
        if lo:
            steer += min(ft_per_mile / lo, 1.0)
        if hi and ft_per_mile > hi:
            steer *= max(STEER_FLOOR, hi / ft_per_mile)
    surface = stretch.data["surface_m"]
    if request.get("min_trail_m") and surface:
        steer += STEER_TRAIL * (surface.get("singletrack", 0.0) + surface.get("track", 0.0)) / sum(surface.values())
    return steer


def plan_blocks(
    conn: sqlite3.Connection, request: route_search.RouteSearchRequest, model: terrain.TerrainModel
) -> BlockPlan:
    """The blocks to build around: those of the sections the request names, or
    of the survey's best sections for its starts, plus the area's other
    stretches as blocks that only earn credit. Takes the search slot (the
    survey does), so call it outside it."""
    area_id = request["area_id"]
    purpose_name = request.get("purpose")
    purpose = survey.purpose_terms(purpose_name)
    preference = request.get("surface_preference") or "any"
    if preference not in survey.SURFACE_WEIGHTS:
        raise ValueError(f"unknown surface_preference {preference!r}; choose from {', '.join(survey.SURFACE_PREFERENCES)}")
    target_m = (request["distance_m_min"] + request["distance_m_max"]) / 2.0
    unfamiliar_only = request.get("unfamiliar_only", False)
    named = list(dict.fromkeys(request.get("sections") or []))
    plan = BlockPlan(explicit=bool(named), gain_band=_gain_band(request, purpose, target_m), fit_gain=purpose_name is not None)
    plan.overrides = {
        r["way_id"]: r["verdict"] for r in conn.execute("SELECT way_id, verdict FROM net_way_overrides").fetchall()
    }

    by_id = {s.section_id: s for s in sections.load_sections(conn, area_id)}
    if not by_id and not named:
        return plan
    ids = named
    if named:
        for section_id in named:
            if section_id not in by_id:
                sections.get_section(conn, section_id)  # raises SectionNotFoundError for an unknown id
                raise ValueError(f"section {section_id} belongs to another area")
    else:
        ids = []
        for start in request["starts"][:MAX_SURVEY_STARTS]:
            result = survey.survey(
                conn, area_id, start["lat"], start["lng"], distance_m=target_m, n=SURVEY_N,
                surface_preference=preference, unfamiliar_only=unfamiliar_only, purpose=purpose_name,
            )
            ids.extend(r["section_id"] for r in result["sections"] if not r["excluded"] and r["section_id"] not in ids)
    judgments = sections.judgments_for(conn, list(by_id.values()))
    if named:
        for section_id in named:
            sec = by_id[section_id]
            parent = by_id.get(sec.group_id) if sec.group_id is not None else None
            if judgments[section_id]["exclusion"] or (parent and judgments[parent.section_id]["exclusion"]):
                raise ValueError(f"section {section_id} is excluded; clear its exclusion to route through it")
    visits = familiarity.visits(conn, area_id) if unfamiliar_only else {}
    classes, speeds = survey.grade_classes(model)

    def planned(stretch: sections.Section, block: sections.Block, runs: bool) -> PlannedBlock:
        factor, _adjustment = survey.section_factor(stretch, by_id[block["section_id"]], judgments)
        density = survey.density_terms(stretch, classes, speeds, preference, purpose).value * factor
        return PlannedBlock(block, density, _steer(stretch, plan.gain_band, request), runs)

    for block in sections.building_blocks(conn, area_id, ids):
        stretch, group = by_id[block["block_id"]], by_id[block["section_id"]]
        if any(v in visits and visits[v].runs > 0 for v in (stretch.section_id, group.section_id)):
            continue
        plan.blocks.append(planned(stretch, block, True))
    built = {pb.block["block_id"] for pb in plan.blocks}
    plan.blocks.extend(
        planned(s, sections.block_of(s), False) for s in by_id.values() if s.level == sections.STRETCH and s.section_id not in built
    )
    if not named:
        for group_id in dict.fromkeys(by_id[i].group_id or i for i in ids):
            covers = [i for i, pb in enumerate(plan.blocks) if pb.runs and pb.block["section_id"] == group_id]
            if covers:
                plan.anchors.append(covers)
            if len(plan.anchors) == ANCHORS:
                break
    for section_id in named:
        covers = [i for i, pb in enumerate(plan.blocks) if section_id in (pb.block["section_id"], pb.block["block_id"])]
        plan.required.append((covers, min(MIN_INCLUDE_M, MIN_INCLUDE_SHARE * by_id[section_id].single_path_m)))
    return plan


def _leaf_keys(key: int, graph: SearchGraph, replaced: Mapping[int, tuple[int, int, int, int]]) -> list[int]:
    if key in graph.edges_by_key:
        return [key]
    pieces = replaced.get(key)
    return [k for piece in pieces[:2] for k in _leaf_keys(piece, graph, replaced)] if pieces else []

# ---------------------------------------------------------------------------
# Connector trees, cached across searches
# ---------------------------------------------------------------------------


class _Space:
    """An area's graph without the avoided ways, and the hub-to-hub connector
    trees computed on it. A tree depends on the route preferences and the
    terrain model through the edge costs, so trees are keyed by a fingerprint
    of those. Used only inside the search lock."""

    def __init__(self, graph: SearchGraph, rows: Mapping[int, dict]) -> None:
        self.graph, self.rows = graph, rows
        self.costs: dict[str, survey.EdgeCosts] = {}
        self.trees: OrderedDict[tuple[str, int], survey.Tree] = OrderedDict()

    def tree(self, fingerprint: str, costs: survey.EdgeCosts, node: int) -> survey.Tree:
        key = (fingerprint, node)
        tree = self.trees.get(key)
        if tree is None:
            tree = self.trees[key] = survey.weighted_tree(self.graph, node, costs, CONNECTOR_MAX_COST_M)
            while len(self.trees) > MAX_CACHED_TREES // MAX_SPACES:
                self.trees.popitem(last=False)
        else:
            self.trees.move_to_end(key)
        return tree


_spaces: OrderedDict[tuple, _Space] = OrderedDict()


def connector_space(
    area_id: int, built_at: str | None, base_graph: SearchGraph, rows: Mapping[int, dict], avoid_way_ids: set[int]
) -> _Space:
    """The area's graph minus `avoid_way_ids`, cached per graph build. Call it
    inside the search lock; callers copy the graph before changing it."""
    key = (area_id, built_at, frozenset(avoid_way_ids))
    space = _spaces.get(key)
    if space is None:
        graph = route_search._copy_graph(base_graph)
        route_search._remove_ways(graph, rows, avoid_way_ids)
        space = _spaces[key] = _Space(graph, rows)
        while len(_spaces) > MAX_SPACES:
            _spaces.popitem(last=False)
    else:
        _spaces.move_to_end(key)
    return space


def _cost_fingerprint(prefs: dict, model: terrain.TerrainModel, overrides: Mapping[int, str]) -> str:
    return json.dumps(
        [prefs, model.walk_onset_grade_pct, model.descent_comfort_grade_pct, sorted(overrides.items())], sort_keys=True
    )


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class _Entry(NamedTuple):
    """Miles on a stretch that earn value: a piece of it, or an edge off the
    chosen stretches. `bit` is what a repeat is counted by, `block` the
    planned block it earns for."""
    bit: int
    value: float
    meters: float
    block: int


class _Run(NamedTuple):
    """Running a piece of a stretch from the node it is listed under."""
    to: int
    edge: GraphEdge
    entry: _Entry
    vert_m: float
    blocks: int


class _Link(NamedTuple):
    """A connector: the path to another hub, its length, its access charge on
    the miles that are on no stretch, and the miles that are on one."""
    to: int
    traversal: Traversal
    length_m: float
    cost: float
    entries: tuple[_Entry, ...]
    vert_m: float
    blocks: int


class _State:
    __slots__ = ("node", "dist_m", "value", "cost", "vert_m", "ran", "twice", "stops", "parent", "action", "bucket", "blocks")

    def __init__(
        self, node: int, dist_m: float, value: float, cost: float, vert_m: float, ran: int, twice: int, stops: int,
        parent: "_State | None", action: _Run | _Link | None, bucket: int,
    ) -> None:
        self.node, self.dist_m, self.value, self.cost, self.vert_m = node, dist_m, value, cost, vert_m
        self.ran, self.twice, self.stops = ran, twice, stops
        self.parent, self.action, self.bucket = parent, action, bucket
        # The planned blocks run so far, one bit each.
        self.blocks = (parent.blocks if parent else 0) | (action.blocks if action else 0)


@dataclass
class Walk:
    """A finished walk, before scoring by the network."""
    start: Start
    traversal: Traversal
    shape: str
    distance_m: float
    value: float
    cost: float
    score: float
    block_m: dict[int, float]
    stops: list[dict]
    # Total vertical travel (climb plus descent) in meters, which is twice a
    # closed route's gain.
    vert_m: float
    # The value before repeats were discounted, and the shape term.
    full_value: float
    shape_fit: float


@dataclass
class _StopSpec:
    node: int
    stop: Stop
    lo_m: float
    hi_m: float


class Assembly:
    """Walks over one search's section graph. The graph, rows and start nodes
    are the search's own (start and stop points spliced in, `replaced`
    recording the cuts); the stretches' edges are cut into pieces in place."""

    def __init__(
        self,
        plan: BlockPlan,
        graph: SearchGraph,
        space: _Space,
        rows: MutableMapping[int, dict],
        replaced: Mapping[int, tuple[int, int, int, int]],
        start_nodes: Sequence[tuple[int, Start]],
        stop_nodes: Sequence[tuple[int, Stop]],
        prefs: dict,
        model: terrain.TerrainModel,
        *,
        distance_m_min: float,
        distance_m_max: float,
        shapes: Sequence[str],
        repeat_value: float,
        ids: Iterator[int],
        cancelled: threading.Event,
    ) -> None:
        self.cancelled = cancelled
        self.plan, self.graph, self.space, self.rows = plan, graph, space, rows
        self.start_nodes, self.prefs, self.ids = list(start_nodes), prefs, ids
        self.dmin, self.dmax = distance_m_min, distance_m_max
        self.target_m = (distance_m_min + distance_m_max) / 2.0
        self.limit_hi = distance_m_max + FIT_SLACK_SHARE * self.target_m
        self.limit_lo = distance_m_min - FIT_SLACK_SHARE * self.target_m
        self.shapes = set(shapes)
        self.repeat_value = repeat_value
        self.costs = survey.EdgeCosts(prefs, model, plan.overrides, rows)
        fingerprint = _cost_fingerprint(prefs, model, plan.overrides)
        self._fingerprint = fingerprint
        self._space_costs = space.costs.setdefault(fingerprint, survey.EdgeCosts(prefs, model, plan.overrides, space.rows))
        self.stops: list[_StopSpec] = []
        for node, stop in stop_nodes:
            at_mi = stop.get("at_mi")
            lo, hi = (0.0, math.inf) if at_mi is None else (
                max(at_mi - STOP_WINDOW_MI, 0.0) * MILE_M, (at_mi + STOP_WINDOW_MI) * MILE_M
            )
            self.stops.append(_StopSpec(node, stop, lo, hi))

        self.hubs: set[int] = set()
        self.runs: dict[int, list[_Run]] = {}
        self._run_origins: set[int] = set()
        self._near_density: dict[int, float] = {}
        self._best_per_mile = 0.0
        # What earns on each stored edge: the block and value per meter of the
        # best stretch it is on; the entries of the pieces cut from it (by the
        # edge they came from, and by key); the entries of the edges off the
        # chosen stretches, once asked for.
        self._credit: dict[int, tuple[int, float]] = {}
        self._origin_entries: dict[int, list[_Entry]] = {}
        self._piece_entry: dict[int, _Entry] = {}
        self._credit_cache: dict[int, tuple[_Entry, ...]] = {}
        self._vertical_m: dict[int, float] = {}
        self._next_bit = 0
        self._build_runs(replaced)
        self._hub_links: dict[int, list[_Link]] = {}
        self._sink_links: dict[int, list[_Link]] = {}
        self._home_m: Mapping[int, float] = {}
        self._worth: dict[int, float] = {}
        self._points: dict[int, list[tuple[float, float]]] = {}

    # -----------------------------------------------------------------------
    # The graph
    # -----------------------------------------------------------------------

    def _cut_into_pieces(self, edge: GraphEdge) -> list[GraphEdge]:
        """`edge` as pieces of about PIECE_M, replacing it in the graph. Rows
        and nodes of the pieces are the search's own; each piece names the
        stored edge it came from."""
        count = math.ceil(edge.length_m / PIECE_M)
        if count <= 1:
            return [edge]
        origin = self.rows[edge.edge_key].get("origin", edge.edge_key)
        pieces: list[GraphEdge] = []
        rest = edge
        for k in range(count - 1):
            split = network.split_edge_row(self.rows[rest.edge_key], rest.length_m / (count - k))
            if split is None:
                break
            mid, first_key, second_key = next(self.ids), next(self.ids), next(self.ids)
            for key, row in ((first_key, split["before"]), (second_key, split["after"])):
                self.rows[key] = row
                row["origin"] = origin
            self.graph.nodes[mid] = NodeInfo(split["node_lat"], split["node_lng"], "junction")
            self.graph.remove_edge(rest)
            first = GraphEdge(first_key, rest.a, mid, split["before"]["length_m"], rest.allowed, [(first_key, True)])
            self.graph.add_edge(first)
            pieces.append(first)
            rest = GraphEdge(second_key, mid, rest.b, split["after"]["length_m"], rest.allowed, [(second_key, True)])
            self.graph.add_edge(rest)
        pieces.append(rest)
        return pieces

    def _vertical(self, key: int) -> float:
        """Climb plus descent along an edge, smoothed as score_path does."""
        found = self._vertical_m.get(key)
        if found is None:
            row = self.rows[key]
            found = 0.0
            if row["has_elevation"]:
                points = network.unpack_edge_points(bytes(row["points_blob"]))
                if len(points) > 1:
                    smoothed, _window = gpx_parse.roughness_smoothed([p[2] for p in points], [cast(float, p[3]) for p in points])
                    found = sum(gpx_parse.gain_loss_m(smoothed))
            self._vertical_m[key] = found
        return found

    def _build_runs(self, replaced: Mapping[int, tuple[int, int, int, int]]) -> None:
        owner: dict[int, int] = {}
        for index, planned in enumerate(self.plan.blocks):
            block = planned.block
            per_m = planned.density / MILE_M
            for eid in block["edge_ids"]:
                held = self._credit.get(eid)
                if held is None or per_m > held[1]:
                    self._credit[eid] = (index, per_m)
            if not planned.runs:
                continue
            self.hubs.update(n for n in (*block["entry_nodes"], *block["end_nodes"]) if n in self.graph.nodes)
            for eid in block["edge_ids"]:
                for key in _leaf_keys(eid, self.graph, replaced):
                    owner.setdefault(key, index)
        for key, index in owner.items():
            per_m = self.plan.blocks[index].density / MILE_M
            for piece in self._cut_into_pieces(self.graph.edges_by_key[key]):
                origin = self.rows[piece.edge_key].get("origin", piece.edge_key)
                self._run_origins.add(origin)
                entry = _Entry(self._next_bit, per_m * piece.length_m, piece.length_m, index)
                self._next_bit += 1
                self._piece_entry[piece.edge_key] = entry
                self._origin_entries.setdefault(origin, []).append(entry)
                vert_m = self._vertical(piece.edge_key)
                for frm in (piece.a, piece.b):
                    step = piece.traverse(frm)
                    if step is not None:
                        self.runs.setdefault(frm, []).append(_Run(step[0], piece, entry, vert_m, 1 << index))
        for node, runs in self.runs.items():
            self._near_density[node] = max(r.entry.value / r.entry.meters for r in runs) * MILE_M
        self._best_per_mile = max(self._near_density.values(), default=0.0)

    def _entries_of(self, key: int) -> tuple[_Entry, ...]:
        """What the miles on an edge earn: nothing off every stretch."""
        found = self._credit_cache.get(key)
        if found is not None:
            return found
        row = self.rows[key]
        origin = row.get("origin", key)
        if key in self._piece_entry:
            found = (self._piece_entry[key],)
        elif origin in self._origin_entries:
            found = tuple(self._origin_entries[origin])
        elif origin in self._credit:
            index, per_m = self._credit[origin]
            found = (_Entry(self._next_bit, per_m * row["length_m"], row["length_m"], index),)
            self._next_bit += 1
        else:
            found = ()
        self._credit_cache[key] = found
        return found

    def _link(
        self, tree: survey.Tree, node: int, to: int, costs: survey.EdgeCosts, rows: Mapping[int, dict]
    ) -> _Link | None:
        """The connector along `tree`'s path between its source and `node`,
        ending at `to`; None when the
        path has more continuous uncomfortable road than the preferences
        allow, or runs on stretches only (a walk takes those as running)."""
        if survey.approach_to(tree, node, costs, rows).max_uncomfortable_run_m > self.prefs["max_continuous_uncomfortable_m"]:
            return None
        traversal = route_search._forward(tree.steps(node))
        if all(self.rows[e.edge_key].get("origin", e.edge_key) in self._run_origins for e, _ in traversal):
            return None
        entries: list[_Entry] = []
        weighted = length = vert_m = 0.0
        for e, _ in traversal:
            found = self._entries_of(e.edge_key)
            entries.extend(found)
            if not found:
                weighted += costs[e.edge_key][0]
            length += e.length_m
            vert_m += self._vertical(e.edge_key)
        by_block: defaultdict[int, float] = defaultdict(float)
        for entry in entries:
            by_block[entry.block] += entry.meters
        blocks = sum(1 << b for b, m in by_block.items() if m >= MIN_INCLUDE_M)
        return _Link(to, traversal, length, CONNECTOR_CHARGE_PER_WEIGHTED_MI * weighted / MILE_M, tuple(entries), vert_m, blocks)

    def _corridor(self, out: survey.Tree) -> set[int]:
        """The edges the paths from a tree's source to the hubs use."""
        keys: set[int] = set()
        seen: set[int] = set()
        for hub in self.hubs:
            node = hub
            while node in out.prev_edge and node not in seen:
                seen.add(node)
                keys.add(out.prev_edge[node].edge_key)
                node = out.prev_node[node]
        return keys

    def _connect_sinks(self, finish: int) -> None:
        """Connectors out of and into the start (`finish`) and the stops. Those into a sink
        come in two kinds: the cheapest path, and the cheapest that avoids the
        corridor the way out uses."""
        sinks = list(dict.fromkeys([finish, *(s.node for s in self.stops)]))
        lengths = {key: (row["length_m"], None, 0.0) for key, row in self.rows.items()}
        self._home_m = survey.weighted_tree(self.graph, finish, lengths, self.limit_hi, inbound=True).cost
        self._sink_links = {}
        bound = APPROACH_TREE_SHARE * self.target_m
        cap = APPROACH_SHARE_MAX * self.target_m
        targets = self.hubs | set(sinks)
        # A walk may turn for home at any junction of the stretches.
        exits = targets | {n for n in self.runs if len(self.graph.adjacency.get(n, ())) != 2}
        for sink in sinks:
            out = survey.weighted_tree(self.graph, sink, self.costs, bound)
            into = survey.weighted_tree(self.graph, sink, self.costs, bound, inbound=True)
            corridor = self._corridor(out)
            around = survey.weighted_tree(self.graph, sink, self.costs, bound, corridor, inbound=True) if corridor else None
            for node in out.cost:
                if node != sink and node in targets and out.length[node] <= cap:
                    link = self._link(out, node, node, self.costs, self.rows)
                    if link is not None:
                        self._sink_links.setdefault(sink, []).append(link)
            for tree in (into, around) if around is not None else (into,):
                for node in tree.cost:
                    if node == sink or node not in exits:
                        continue
                    if tree is around and [e.edge_key for e, _, _ in into.steps(node)] == [e.edge_key for e, _, _ in tree.steps(node)]:
                        continue
                    link = self._link(tree, node, sink, self.costs, self.rows)
                    if link is not None:
                        self._sink_links.setdefault(node, []).append(link)

    def _links_of_hub(self, node: int) -> list[_Link]:
        links = self._hub_links.get(node)
        if links is None:
            tree = self.space.tree(self._fingerprint, self._space_costs, node)
            found = [
                link for other in tree.cost
                if other != node and other in self.hubs and other in self.graph.nodes
                and (link := self._link(tree, other, other, self._space_costs, self.space.rows)) is not None
            ]
            found.sort(key=lambda link: (link.cost, link.to))
            links = self._hub_links[node] = found[:CONNECTORS_PER_HUB]
        return links

    def _links(self, node: int) -> list[_Link]:
        links = self._sink_links.get(node, [])
        return links + self._links_of_hub(node) if node in self.hubs else links

    # -----------------------------------------------------------------------
    # The search
    # -----------------------------------------------------------------------

    def _fit(self, distance_m: float) -> float:
        miss = max(self.dmin - distance_m, distance_m - self.dmax, 0.0)
        return max(0.0, 1.0 - FIT_SLOPE * miss / max(self.target_m, 1.0))

    def _gain_fit(self, gain_m: float, distance_m: float) -> float:
        if not self.plan.fit_gain:
            return 1.0
        return survey.gain_fit(gain_m * FT_PER_M / (max(distance_m, 1.0) / MILE_M), self.plan.gain_band)

    @staticmethod
    def _per_mile(value: float, cost: float, distance_m: float) -> float:
        return (value - cost) / max(distance_m / MILE_M, 1e-9)

    def _score(self, state: _State) -> float:
        return self._per_mile(state.value, state.cost, state.dist_m) * self._fit(state.dist_m) * self._gain_fit(state.vert_m / 2.0, state.dist_m)

    def _shape_fit(self, segments: Sequence[tuple[int, bool]]) -> float:
        """The shape term of a route (see the module docstring)."""
        if not segments:
            return 1.0
        runs = Counter(key for key, _ in segments)
        first = self._edge_points(segments[0][0])
        lat0, lng0 = first[0] if segments[0][1] else first[-1]
        kx = math.cos(math.radians(lat0)) * routes_spatial.METERS_PER_DEG_LAT
        points: list[tuple[float, float, float, float, bool]] = []
        arc = reach = 0.0
        for key, forward in segments:
            raw = self._edge_points(key)
            if not forward:
                raw = raw[::-1]
            length = self.rows[key]["length_m"]
            step = length / max(len(raw) - 1, 1)
            for i, (lat, lng) in enumerate(raw):
                x, y = (lng - lng0) * kx, (lat - lat0) * routes_spatial.METERS_PER_DEG_LAT
                reach = max(reach, math.hypot(x, y))
                if i < len(raw) - 1:
                    points.append((x, y, arc + i * step, step, runs[key] > 1))
            arc += length
        if arc <= 0:
            return 1.0
        cells: defaultdict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (x, y, _s, _w, repeated) in enumerate(points):
            if not repeated:
                cells[int(x // PROXIMITY_M), int(y // PROXIMITY_M)].append(i)
        near_m = 0.0
        for members in cells.values():
            for i in members:
                x, y, s, w, _r = points[i]
                cx, cy = int(x // PROXIMITY_M), int(y // PROXIMITY_M)
                if any(
                    min(abs(points[j][2] - s), arc - abs(points[j][2] - s)) >= PROXIMITY_APART_M
                    and math.hypot(points[j][0] - x, points[j][1] - y) <= PROXIMITY_M
                    for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in cells.get((cx + dx, cy + dy), ())
                ):
                    near_m += w
        openness = SHAPE_MIN + (SHAPE_MAX - SHAPE_MIN) * min(reach / arc / OPEN_REACH, 1.0)
        return min(max(openness * (1.0 - near_m / arc), SHAPE_MIN), SHAPE_MAX)

    def _edge_points(self, key: int) -> list[tuple[float, float]]:
        found = self._points.get(key)
        if found is None:
            row = self.rows[key]
            found = self._points[key] = [(p[0], p[1]) for p in network.unpack_edge_points(bytes(row["points_blob"]))]
        return found

    def _earn(self, state: _State, entries: Sequence[_Entry]) -> tuple[float, int, int]:
        """What running `entries` after `state` earns, and the repeat counts after."""
        gain, ran, twice = 0.0, state.ran, state.twice
        for entry in entries:
            bit = 1 << entry.bit
            if twice & bit:
                factor = self.repeat_value**2
            elif ran & bit:
                factor, twice = self.repeat_value, twice | bit
            else:
                factor, ran = 1.0, ran | bit
            gain += entry.value * factor * self._worth.get(entry.block, 1.0)
        return gain, ran, twice

    def _rank(self, state: _State) -> tuple[float, int, float]:
        """Sort key, best first: the score so far plus what the miles left
        could earn at the best density within reach of the node."""
        density = self._near_density.get(state.node, self._best_per_mile)
        bound = max(self.target_m - state.dist_m - self._home_m.get(state.node, 0.0), 0.0) / MILE_M * density
        return (-(state.value - state.cost + bound), state.node, state.bucket)

    def _child(
        self, state: _State, to: int, length_m: float, gain: float, cost: float, action: _Run | _Link,
        ran: int, twice: int,
    ) -> _State | None:
        dist = state.dist_m + length_m
        if dist > self.limit_hi:
            return None
        home = self._home_m.get(to)
        if home is None:
            return None
        if dist + home > self.limit_hi:
            return None
        stops = state.stops
        for i, spec in enumerate(self.stops):
            if stops >> i & 1:
                continue
            if spec.node == to and spec.lo_m <= dist <= spec.hi_m:
                stops |= 1 << i
            elif dist > spec.hi_m:
                return None
        bucket = max(state.bucket + 1, int(dist // BUCKET_M))
        return _State(
            to, dist, state.value + gain, state.cost + cost, state.vert_m + action.vert_m, ran, twice, stops, state, action, bucket
        )

    def _beam(self, start_node: int, deadline: float) -> list[_State]:
        """The finished walks from one start: states at the start, inside the
        range (with slack) and past every stop."""
        everything = (1 << len(self.stops)) - 1
        root = _State(start_node, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, None, None, 0)
        layers: dict[int, dict[tuple[int, int], _State]] = {0: {(start_node, 0): root}}
        done: list[_State] = []
        while layers and time.monotonic() <= deadline and not self.cancelled.is_set():
            layer = layers.pop(min(layers))
            for state in sorted(layer.values(), key=self._rank)[:BEAM_WIDTH]:
                children: list[_State | None] = []
                for run in self.runs.get(state.node, ()):
                    gain, ran, twice = self._earn(state, (run.entry,))
                    children.append(self._child(state, run.to, run.edge.length_m, gain, 0.0, run, ran, twice))
                for link in self._links(state.node):
                    gain, ran, twice = self._earn(state, link.entries)
                    children.append(self._child(state, link.to, link.length_m, gain, link.cost, link, ran, twice))
                for child in children:
                    if child is None:
                        continue
                    if child.node == start_node and child.stops == everything and self.limit_lo <= child.dist_m:
                        done.append(child)
                    held = layers.setdefault(child.bucket, {})
                    key = (child.node, child.stops)
                    if key not in held or child.value - child.cost > held[key].value - held[key].cost:
                        held[key] = child
        return done

    # -----------------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------------

    def walks(self, deadline: float) -> Iterator[Walk]:
        """Distinct finished walks, best first, until `deadline`. Each start
        gets an equal share of the time. A start is searched up to ROUNDS times,
        each time with the stretches of the best walk so far worth ROUND_DISCOUNT
        of what they were, so the walks found are not all variations of one."""
        found: list[Walk] = []
        covers: list[Mapping[int, float]] = []
        for k, (node, start) in enumerate(self.start_nodes):
            now = time.monotonic()
            share_end = now + (deadline - now) / (len(self.start_nodes) - k)
            self._worth = {i: planned.steer for i, planned in enumerate(self.plan.blocks)}
            self._connect_sinks(node)
            for round_no in range(ROUNDS):
                anchor = self.plan.anchors[round_no - 1] if 1 <= round_no <= len(self.plan.anchors) else ()
                discounted = self._worth
                self._worth = {**discounted, **{b: discounted[b] * ANCHOR_BOOST for b in anchor}}
                fresh: list[Walk] = []
                states = sorted(self._beam(node, share_end), key=lambda s: (-self._score(s), s.dist_m))
                for state in itertools.islice(_by_variety(states, lambda s: s.blocks), SCANNED_COMPLETIONS):
                    walk = self._walk(state, start)
                    if not walk.block_m or walk.shape not in self.shapes or not self._includes_required(walk):
                        continue
                    cover = route_search._cover(self.rows, route_search._flatten(walk.traversal))
                    if any(route_search._duplicate(cover, other) for other in covers):
                        continue
                    covers.append(cover)
                    fresh.append(walk)
                    if len(fresh) >= MAX_COMPLETIONS:
                        break
                self._worth = discounted
                found.extend(fresh)
                if self.cancelled.is_set() or not (fresh or anchor):
                    break
                if fresh:
                    for block in max(fresh, key=lambda w: w.score).block_m:
                        self._worth[block] = self._worth[block] * ROUND_DISCOUNT
        found.sort(key=lambda w: -w.score)
        yield from itertools.islice(_by_variety(found, lambda w: frozenset(i for i, m in w.block_m.items() if m >= MIN_INCLUDE_M)), MAX_WALKS)

    def _includes_required(self, walk: Walk) -> bool:
        return all(sum(walk.block_m.get(i, 0.0) for i in covers) >= least_m for covers, least_m in self.plan.required)

    def _walk(self, state: _State, start: Start) -> Walk:
        """The walk that ends in `state`, valued without any round discount."""
        chain: list[_State] = [state]
        while chain[-1].parent is not None:
            chain.append(chain[-1].parent)
        chain.reverse()
        traversal: Traversal = []
        block_m: defaultdict[int, float] = defaultdict(float)
        passes: Counter[int] = Counter()
        value = full_value = 0.0
        for before, s in zip(chain, chain[1:]):
            action = s.action
            if isinstance(action, _Run):
                traversal.append((action.edge, before.node))
                entries: Sequence[_Entry] = (action.entry,)
            elif action is not None:
                traversal.extend(action.traversal)
                entries = action.entries
            else:
                continue
            for entry in entries:
                block_m[entry.block] += entry.meters
                value += entry.value * self.repeat_value ** min(passes[entry.bit], 2)
                full_value += entry.value
                passes[entry.bit] += 1
        reached: list[dict] = []
        for spec in self.stops:
            visit = next((s for s in chain if s.node == spec.node and spec.lo_m <= s.dist_m <= spec.hi_m), None)
            reached.append({
                "label": spec.stop.get("label"), "lat": spec.stop["lat"], "lng": spec.stop["lng"],
                "wanted_mi": spec.stop.get("at_mi"), "at_mi": None if visit is None else visit.dist_m / MILE_M,
            })
        shape_fit = self._shape_fit(route_search._flatten(traversal))
        score = self._per_mile(value, state.cost, state.dist_m) * self._fit(state.dist_m) * self._gain_fit(state.vert_m / 2.0, state.dist_m) * shape_fit
        return Walk(start, traversal, _shape(traversal), state.dist_m, value, state.cost, score, block_m, reached, state.vert_m, full_value, shape_fit)

    def rank_path(self, start: Start, segments: Sequence[tuple[int, bool]], distance_m: float, gain_m: float | None) -> dict:
        """The terms of a path's score that was not built by this search: the
        stretch value of the miles it runs, repeats discounted, and the charge
        on the rest, as `rank` gives a walk."""
        passes: Counter[int] = Counter()
        block_m: defaultdict[int, float] = defaultdict(float)
        value = full_value = weighted = vert_m = 0.0
        for key, _ in segments:
            vert_m += self._vertical(key)
            entries = self._entries_of(key)
            if not entries:
                weighted += self.costs[key][0]
            for entry in entries:
                block_m[entry.block] += entry.meters
                value += entry.value * self.repeat_value ** min(passes[entry.bit], 2)
                full_value += entry.value
                passes[entry.bit] += 1
        cost = CONNECTOR_CHARGE_PER_WEIGHTED_MI * weighted / MILE_M
        walk = Walk(start, [], "", distance_m, value, cost, 0.0, block_m, [], vert_m, full_value, self._shape_fit(segments))
        return self.rank(walk, gain_m)

    def rank(self, walk: Walk, gain_m: float | None) -> dict:
        """The terms of a walk's score and the sections it runs, the climb
        being the route's own when it is known and else the walk's estimate.
"""
        fit = self._fit(walk.distance_m)
        gain_fit = self._gain_fit(walk.vert_m / 2.0 if gain_m is None else gain_m, walk.distance_m)
        used = [
            {
                "section_id": self.plan.blocks[i].block["section_id"],
                "block_id": self.plan.blocks[i].block["block_id"],
                "name": self.plan.blocks[i].block["name"],
                "length_m": m,
            }
            for i, m in sorted(walk.block_m.items(), key=lambda kv: -kv[1])
            if m >= MIN_INCLUDE_M
        ]
        miles = max(walk.distance_m / MILE_M, 1e-9)
        value_per_mile, cost_per_mile = walk.value / miles, walk.cost / miles
        return {
            "score": (value_per_mile - cost_per_mile) * fit * gain_fit * walk.shape_fit,
            "quality_mi": walk.value,
            "value_per_mile": value_per_mile,
            "connector_cost_per_mile": cost_per_mile,
            "repeats_effect": walk.value / walk.full_value if walk.full_value > 0 else 1.0,
            "distance_fit": fit,
            "gain_fit": gain_fit,
            "shape": walk.shape_fit,
            "access_cost": walk.cost,
            "sections_used": used,
        }


def _by_variety(walks: Sequence[_T], key: Callable[[_T], Hashable]) -> Iterator[_T]:
    """`walks` (best first) taking the best of each group by `key`, then the
    second best of each, and so on. Walks that differ only in where they turn
    on one stretch would otherwise crowd out the best walk over another set of
    stretches, in the scan for completions and in the caller's pool."""
    groups: defaultdict[Hashable, list[_T]] = defaultdict(list)
    for walk in walks:
        groups[key(walk)].append(walk)
    return (w for tier in itertools.zip_longest(*groups.values()) for w in tier if w is not None)


def _shape(traversal: Traversal) -> str:
    """What a finished walk looks like: an out-and-back when it repeats
    OUT_AND_BACK_REUSE_SHARE of its length, a lollipop when its end retraces its
    start for MIN_STEM_M or more, otherwise a loop."""
    keys = [edge.edge_key for edge, _ in traversal]
    seen: set[int] = set()
    repeated = total = 0.0
    for key, (edge, _) in zip(keys, traversal):
        total += edge.length_m
        if key in seen:
            repeated += edge.length_m
        seen.add(key)
    if total > 0 and repeated / total >= OUT_AND_BACK_REUSE_SHARE:
        return "out_and_back"
    stem = 0.0
    i, j = 0, len(keys) - 1
    while i < j and keys[i] == keys[j]:
        stem += traversal[i][0].length_m
        i, j = i + 1, j - 1
    return "lollipop" if stem >= MIN_STEM_M else "loop"
