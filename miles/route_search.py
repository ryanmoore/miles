"""Route search over an area's network graph (adr/0003-terrain-model-and-route-search.md,
adr/0004-section-first-route-search.md): bound the graph around each start,
build candidates around chosen sections (assemble.py) or, when none qualify,
by randomized walks over the contracted graph, score each through
network.score_path, and diversify.

A search never writes to the network graph: a start point is spliced into an
in-memory copy. Candidates are kept behind a TTL until saved
(save_route_candidate) into the routes table.
"""

import hashlib
import heapq
import itertools
import json
import os
import random
import sqlite3
import threading
import time
import uuid
from collections import ChainMap, OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Collection, Iterable, Iterator, Literal, Mapping, MutableMapping, NamedTuple, Sequence, cast
from typing_extensions import NotRequired, TypedDict

from . import areas, drive_starts, network, routes, routes_spatial, sections, terrain
from .fitness import FT_PER_M, MILE_M

Shape = Literal["loop", "out_and_back", "lollipop", "any"]

DEFAULT_N = 5
DEFAULT_TIME_BUDGET_S = 10.0
# Overrides DEFAULT_TIME_BUDGET_S for requests that give no time_budget_s, for
# hardware too slow to finish a search in the default.
BUDGET_ENV = "MILES_SEARCH_BUDGET_S"
CANDIDATE_TTL_HOURS = 24.0

# How far a request's start may snap onto an edge; wider than
# AMENITY_SNAP_RADIUS_M because an athlete-given point is less precise.
START_SNAP_RADIUS_M = 500.0

BEAM_WIDTH = 4
# Step-count backstop against pathological wandering; edge length varies too
# much between street grids and trail networks to bound steps tighter.
LOOP_STEP_CEILING = 600
# A loop walk gives up after covering this multiple of the target distance.
LOOP_MAX_OVERRUN = 1.6
# A walk turns home once the distance left to the target is within this
# multiple of the shortest path back.
TURN_HOME_SLACK = 1.15
# Gate on the exact distance-home check: skipped when the straight-line
# distance to the start, inflated by this factor, already rules a turn out.
# Must stay loose enough not to skip the step where turning home becomes
# possible.
HOME_ESTIMATE_INFLATION = 1.75
# Fraction of the target distance an out-and-back turnaround or a lollipop's
# stem may land within and still be accepted.
TURNAROUND_TOLERANCE = 0.25
LOLLIPOP_STEM_FRACTION = 0.25
MAX_GENERATION_ATTEMPTS = 300
# Candidates to generate per requested result before diversifying down.
POOL_FACTOR = 3
# Consecutive attempts adding no new qualifying candidate (once n qualify)
# after which a small network is judged exhausted.
STAGNATION_ATTEMPTS = 40
DIVERSITY_WEIGHT = 2.0
# A section-built candidate scoring under this share of the best in the pool is
# only offered after every candidate above it, however different from the rest.
DIVERSITY_FLOOR = 0.25
# The share of the time budget the section search may use when the random walk
# could still have to fill in.
SECTIONS_BUDGET_SHARE = 0.6
# Two walks sharing this share of the longer one's length by original edge,
# whatever point a piece was cut at, are one walk (assemble.py drops the
# second); the meters each runs on an edge count, so a retraced stretch is not
# the same walk as a single pass.
DUPLICATE_SHARE = 0.95
# Two candidates are similar when more than this share of the shorter one's
# ground lies within about FOOTPRINT_CELL_M of the other; a returned shortlist
# never holds two.
SIMILAR_SHARE = 0.6
FOOTPRINT_CELL_M = 30.0
# A stretch run again earns this share of its value; the request may set it
# from 0 to 1.
DEFAULT_REPEAT_VALUE = 0.5


class Start(TypedDict):
    lat: float
    lng: float
    label: NotRequired[str | None]
    drive_s: NotRequired[float | None]
    drive_time_estimated: NotRequired[bool]


class Stop(TypedDict):
    lat: float
    lng: float
    label: NotRequired[str | None]
    # Where along the route the stop should come, in miles; absent, anywhere.
    at_mi: NotRequired[float | None]


class RouteSearchRequest(TypedDict):
    area_id: int
    starts: list[Start]
    distance_m_min: float
    distance_m_max: float
    shape: NotRequired[Shape]
    min_trail_m: NotRequired[float | None]
    min_runnable_pct: NotRequired[float | None]
    gain_m_min: NotRequired[float | None]
    gain_m_max: NotRequired[float | None]
    include_way_ids: NotRequired[list[int] | None]
    avoid_way_ids: NotRequired[list[int] | None]
    n: NotRequired[int]
    time_budget_s: NotRequired[float]
    seed: NotRequired[int | None]
    # Section ids (group, piece, or stretch) to build routes around; absent,
    # the survey's best sections for the start are used.
    sections: NotRequired[list[int] | None]
    repeat_value: NotRequired[float]
    stops: NotRequired[list[Stop] | None]
    surface_preference: NotRequired[str | None]
    unfamiliar_only: NotRequired[bool]
    # What the run is for (survey.PURPOSES): sets the climb per mile a route is
    # scored against and how the survey and the search weigh sections. An
    # explicit gain range overrides the climb band.
    purpose: NotRequired[str | None]


class ConstraintMiss(TypedDict):
    constraint: str
    detail: str


class RouteCandidateOut(TypedDict):
    candidate_id: str
    area_id: int
    shape: str
    start: Start
    distance_m: float
    score: dict
    missed_constraints: list[ConstraintMiss]
    geometry: list[list[float | None]]  # [lat, lng, ele_m|None] triples
    ways: list[dict]  # consecutive runs of one OSM way: way_id, name, surface_class, length_m
    # "sections" for candidates assembled around sections, "walk" for the
    # randomized fallback; absent on candidates stored before sections existed.
    built_from: NotRequired[str]
    sections_used: NotRequired[list[dict]]  # section_id, block_id, name, length_m
    ranking: NotRequired[dict]  # the terms of the candidate's score
    stops: NotRequired[list[dict]]  # each requested stop with the mile it was passed at


class RouteSearchResult(TypedDict):
    candidates: list[RouteCandidateOut]
    near_miss: bool
    budget_exceeded: bool
    seed: int
    warnings: list[str]


class SearchProgress:
    """What another thread may read of a running search, and its cancel flag.
    `started` is set once the search holds the search lock; `qualifying` is
    the pool's size; `snapshot`, set once there is a pool, persists and returns
    the best candidates so far."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.started = False
        self.qualifying = 0
        self.snapshot: Callable[[sqlite3.Connection], list[RouteCandidateOut]] | None = None


class CandidateNotFoundError(LookupError):
    pass


# ---------------------------------------------------------------------------
# Schema / connection
# ---------------------------------------------------------------------------


def init_search_schema(conn: sqlite3.Connection) -> None:
    # data is the candidate as get_route_candidate returns it, minus its id.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_candidates (
            candidate_id TEXT PRIMARY KEY,
            expires_at   TEXT NOT NULL,
            data         TEXT NOT NULL
        )
    """)


def _purge_expired(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM net_candidates WHERE expires_at < ?", [datetime.now(timezone.utc).isoformat()])
    conn.commit()


# ---------------------------------------------------------------------------
# In-memory search graph
# ---------------------------------------------------------------------------


@dataclass
class NodeInfo:
    lat: float
    lng: float
    kind: str


@dataclass
class GraphEdge:
    """One traversable connection between two search-graph nodes.

    edge_key is a net_edges id for an untouched edge, or a negative synthetic
    id for a start-split piece or a degree-2-chain contraction (contractions
    are never scored directly). `segments` lists the real (edge_id, forward)
    steps for walking from `a` to `b`; forward says whether a step reads its
    points_blob in stored order, independent of this edge's a/b labels.
    """
    edge_key: int
    a: int
    b: int
    length_m: float
    allowed: Literal["both", "a_to_b", "b_to_a"]
    segments: list[tuple[int, bool]]

    def traverse(self, frm: int) -> tuple[int, list[tuple[int, bool]]] | None:
        if frm == self.a:
            if self.allowed == "b_to_a":
                return None
            return self.b, self.segments
        if frm == self.b:
            if self.allowed == "a_to_b":
                return None
            return self.a, [(eid, not fwd) for eid, fwd in reversed(self.segments)]
        raise ValueError(f"edge {self.edge_key} doesn't touch node {frm}")


@dataclass
class SearchGraph:
    nodes: dict[int, NodeInfo] = field(default_factory=dict)
    adjacency: dict[int, list[GraphEdge]] = field(default_factory=dict)
    edges_by_key: dict[int, GraphEdge] = field(default_factory=dict)

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges_by_key[edge.edge_key] = edge
        self.adjacency.setdefault(edge.a, []).append(edge)
        self.adjacency.setdefault(edge.b, []).append(edge)

    def remove_edge(self, edge: GraphEdge) -> None:
        del self.edges_by_key[edge.edge_key]
        self.adjacency[edge.a] = [e for e in self.adjacency.get(edge.a, []) if e is not edge]
        self.adjacency[edge.b] = [e for e in self.adjacency.get(edge.b, []) if e is not edge]


_ALLOWED_BY_ONEWAY: dict[str, Literal["both", "a_to_b", "b_to_a"]] = {
    "no": "both", "yes": "a_to_b", "-1": "b_to_a",
}


def _load_graph(conn: sqlite3.Connection, area_id: int) -> tuple[SearchGraph, dict[int, dict]]:
    """The area's full graph plus edge_rows, a lookup for geometry and way ids
    and for score_path's edges that are not in the database."""
    graph = SearchGraph()
    edge_rows: dict[int, dict] = {}
    for row in conn.execute("SELECT * FROM net_nodes WHERE area_id = ?", [area_id]).fetchall():
        graph.nodes[row["node_id"]] = NodeInfo(row["lat"], row["lng"], row["kind"])
    for row in conn.execute("SELECT * FROM net_edges WHERE area_id = ?", [area_id]).fetchall():
        edge_id = row["edge_id"]
        edge_rows[edge_id] = dict(row)
        allowed = _ALLOWED_BY_ONEWAY.get(row["oneway"], "both")
        graph.add_edge(GraphEdge(edge_id, row["from_node_id"], row["to_node_id"], row["length_m"], allowed, [(edge_id, True)]))
    return graph, edge_rows


# Loading an area's graph dwarfs the cost of searching it, so graphs are cached
# per area, keyed on net_graphs.built_at so a rebuild invalidates the entry.
_graph_cache: OrderedDict[int, tuple[str | None, SearchGraph, dict[int, dict]]] = OrderedDict()
# Each entry is a whole area's graph in memory.
_GRAPH_CACHE_MAX_AREAS = 3


def _built_at(conn: sqlite3.Connection, area_id: int) -> str | None:
    row = conn.execute("SELECT built_at FROM net_graphs WHERE area_id = ?", [area_id]).fetchone()
    return row["built_at"] if row is not None else None


# Candidate generation is CPU-bound against a wall-clock budget, so concurrent
# searches would each get a fraction of their budget's computation. One runs at
# a time per process and the others queue; each budget starts on acquisition.
# Graph loading happens before the lock is taken and no loader ever takes it,
# so the lock cannot take part in a cycle with the cache locks below.
_search_lock = threading.Lock()

# Searches run on worker threads. The first lock guards the cache and the build
# locks and is never held during a load; a build lock lets concurrent searches
# of one area share a single load.
_graph_cache_lock = threading.Lock()
_graph_build_locks: dict[int, threading.Lock] = {}


def _cache_hit(area_id: int, built_at: str | None) -> tuple[SearchGraph, dict[int, dict]] | None:
    with _graph_cache_lock:
        cached = _graph_cache.get(area_id)
        if cached is None or cached[0] != built_at:
            return None
        _graph_cache.move_to_end(area_id)
        return cached[1], cached[2]


def _cached_base_graph(conn: sqlite3.Connection, area_id: int) -> tuple[SearchGraph, dict[int, dict]]:
    built_at = _built_at(conn, area_id)
    hit = _cache_hit(area_id, built_at)
    if hit is not None:
        return hit
    with _graph_cache_lock:
        build_lock = _graph_build_locks.setdefault(area_id, threading.Lock())
    with build_lock:
        hit = _cache_hit(area_id, built_at)
        if hit is not None:
            return hit
        graph, edge_rows = _load_graph(conn, area_id)
        with _graph_cache_lock:
            _graph_cache[area_id] = (built_at, graph, edge_rows)
            _graph_cache.move_to_end(area_id)
            while len(_graph_cache) > _GRAPH_CACHE_MAX_AREAS:
                evicted, _ = _graph_cache.popitem(last=False)
                _graph_build_locks.pop(evicted, None)
    return graph, edge_rows


def _copy_graph(graph: SearchGraph) -> SearchGraph:
    """A per-request working copy. GraphEdge/NodeInfo are never mutated once
    built, so only the indexes are copied."""
    copy = SearchGraph()
    copy.nodes = dict(graph.nodes)
    copy.adjacency = {node: list(edges) for node, edges in graph.adjacency.items()}
    copy.edges_by_key = dict(graph.edges_by_key)
    return copy


def _remove_ways(graph: SearchGraph, edge_rows: Mapping[int, dict], way_ids: set[int]) -> None:
    """Drop every edge belonging to one of way_ids."""
    if not way_ids:
        return
    for edge in list(graph.edges_by_key.values()):
        row = edge_rows.get(edge.edge_key)
        if row is not None and row["way_id"] in way_ids:
            graph.remove_edge(edge)


def _bound_graph(
    graph: SearchGraph, centers: Sequence[tuple[float, float]], radius_m: float, keep_nodes: Collection[int] = ()
) -> None:
    """Drop every node farther than radius_m from all centers, and any edge that
    loses an endpoint. Straight-line distance stands in for graph distance,
    which isn't known until the graph is built. keep_nodes survive regardless:
    a section's long edge must not vanish because its far end lies outside."""
    def _keep(node: NodeInfo) -> bool:
        return any(routes_spatial.equirect_distance_m(node.lat, node.lng, clat, clng) <= radius_m for clat, clng in centers)

    kept = set(keep_nodes)
    dropped = {n for n, info in graph.nodes.items() if n not in kept and not _keep(info)}
    for edge in list(graph.edges_by_key.values()):
        if edge.a in dropped or edge.b in dropped:
            graph.remove_edge(edge)
    for n in dropped:
        graph.nodes.pop(n, None)
        graph.adjacency.pop(n, None)


# ---------------------------------------------------------------------------
# Splicing a search's own start point into the graph
# ---------------------------------------------------------------------------


def _edge_index(edge_rows: Mapping[int, dict], edge_ids: Iterable[int]) -> routes_spatial.NearestIndex:
    """The sample points of the given edges, keyed by edge id."""
    index = routes_spatial.NearestIndex(cell_size_m=50.0)
    for eid in edge_ids:
        network.index_edge(index, eid, edge_rows[eid]["points_blob"])
    return index


def _splice_start(
    graph: SearchGraph, edge_rows: MutableMapping[int, dict], index: routes_spatial.NearestIndex,
    lat: float, lng: float, ids: Iterable[int], replaced: MutableMapping[int, tuple[int, int, int, int]] | None = None,
) -> int | None:
    """Snap (lat, lng) onto the graph's nearest edge and cut it there, in memory
    only. index covers the graph's current edges and is updated with each cut.
    replaced, if given, records each cut edge's key against its two pieces'
    keys (before, after, in the cut edge's stored direction) and its end
    nodes (a, b). Returns the
    start's node id, or None when nothing is within START_SNAP_RADIUS_M.
    """
    cut = network.splice_at(edge_rows, index, lat, lng, START_SNAP_RADIUS_M, ids)
    if cut is None:
        return None
    edge = graph.edges_by_key[cut.edge_key]
    split = cut.split
    if split is None:
        return edge.a if cut.offset_m < cut.row["length_m"] / 2.0 else edge.b

    for key in (cut.before_key, cut.after_key):
        edge_rows[key]["origin"] = cut.row.get("origin", cut.edge_key)
    node_id = next(iter(ids))
    graph.nodes[node_id] = NodeInfo(split["node_lat"], split["node_lng"], "start")
    if replaced is not None:
        replaced[cut.edge_key] = (cut.before_key, cut.after_key, edge.a, edge.b)
    graph.remove_edge(edge)
    graph.add_edge(
        GraphEdge(cut.before_key, edge.a, node_id, split["before"]["length_m"], edge.allowed, [(cut.before_key, True)])
    )
    graph.add_edge(
        GraphEdge(cut.after_key, node_id, edge.b, split["after"]["length_m"], edge.allowed, [(cut.after_key, True)])
    )
    return node_id


# ---------------------------------------------------------------------------
# Degree-2 chain contraction
# ---------------------------------------------------------------------------


def contract_degree2_chains(graph: SearchGraph, ids: Iterable[int], protected: Collection[int] = ()) -> None:
    """Collapse junction nodes with exactly two incident edges into one edge.
    Amenity and start nodes are never touched, and `protected` lists further
    nodes that must survive (a start snapped onto an existing junction)."""
    keep = set(protected)
    queue: list[int] = [
        n for n, edges in graph.adjacency.items()
        if graph.nodes[n].kind == "junction" and len(edges) == 2 and n not in keep
    ]
    queued = set(queue)
    while queue:
        node = queue.pop()
        queued.discard(node)
        edges = graph.adjacency.get(node, [])
        if node not in graph.nodes or graph.nodes[node].kind != "junction" or len(edges) != 2 or node in keep:
            continue
        e1, e2 = edges
        if e1.allowed != "both" or e2.allowed != "both":
            continue
        step1 = e1.traverse(node)
        step2 = e2.traverse(node)
        assert step1 is not None and step2 is not None
        n1, seg1 = step1
        n2, seg2 = step2
        if n1 == n2:
            continue  # a dangling loop back to the same neighbor; leave it
        merged_segments = [(eid, not fwd) for eid, fwd in reversed(seg1)] + seg2
        merged = GraphEdge(next(iter(ids)), n1, n2, e1.length_m + e2.length_m, "both", merged_segments)
        graph.remove_edge(e1)
        graph.remove_edge(e2)
        graph.adjacency.pop(node, None)
        graph.add_edge(merged)
        for neighbor in (n1, n2):
            if (
                neighbor in graph.nodes and graph.nodes[neighbor].kind == "junction" and neighbor not in keep
                and len(graph.adjacency.get(neighbor, [])) == 2 and neighbor not in queued
            ):
                queue.append(neighbor)
                queued.add(neighbor)


# ---------------------------------------------------------------------------
# Shortest paths
# ---------------------------------------------------------------------------


def _dijkstra(
    graph: SearchGraph, source: int, target: int | None = None, max_dist: float | None = None
) -> tuple[dict[int, float], dict[int, GraphEdge], dict[int, int]]:
    """max_dist prunes the search once the frontier passes it (safe: pops come
    in non-decreasing distance)."""
    dist: dict[int, float] = {source: 0.0}
    prev_edge: dict[int, GraphEdge] = {}
    prev_node: dict[int, int] = {}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if node in visited:
            continue
        if max_dist is not None and d > max_dist:
            break
        visited.add(node)
        if target is not None and node == target:
            break
        for edge in graph.adjacency.get(node, []):
            step = edge.traverse(node)
            if step is None:
                continue
            nxt, _segs = step
            nd = d + edge.length_m
            if nxt not in dist or nd < dist[nxt]:
                dist[nxt] = nd
                prev_edge[nxt] = edge
                prev_node[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    return dist, prev_edge, prev_node


def _reconstruct(target: int, prev_edge: dict[int, GraphEdge], prev_node: dict[int, int]) -> list[tuple[GraphEdge, int, int]]:
    """Steps (edge, from_node, to_node) for the path a _dijkstra(graph,
    source) call found ending at target, in travel order (source first)."""
    steps: list[tuple[GraphEdge, int, int]] = []
    node = target
    while node in prev_edge:
        edge = prev_edge[node]
        frm = prev_node[node]
        steps.append((edge, frm, node))
        node = frm
    steps.reverse()
    return steps


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

Traversal = list[tuple[GraphEdge, int]]  # (edge, from_node) in travel order

ShortestPathTree = tuple[dict[int, float], dict[int, GraphEdge], dict[int, int]]


@dataclass
class _SearchContext:
    """Per-search state shared across attempts. The shortest-path tree depends
    only on (start_node, outbound target), both fixed for the search, so it is
    memoized."""
    graph: SearchGraph
    _trees: dict[tuple[int, float], ShortestPathTree] = field(default_factory=dict)

    def tree(self, start_node: int, outbound_target: float) -> ShortestPathTree:
        """The shortest-path tree from start_node, pruned to what a turnaround
        near outbound_target can need."""
        key = (start_node, outbound_target)
        if key not in self._trees:
            self._trees[key] = _dijkstra(self.graph, start_node, max_dist=outbound_target * (1.0 + TURNAROUND_TOLERANCE))
        return self._trees[key]


def _generate_loop(ctx: _SearchContext, start_node: int, target_m: float, rng: random.Random) -> Traversal | None:
    graph = ctx.graph
    start_info = graph.nodes[start_node]
    current = start_node
    dist_so_far = 0.0
    traversal: Traversal = []
    steps = 0
    while steps < LOOP_STEP_CEILING and dist_so_far <= target_m * LOOP_MAX_OVERRUN:
        steps += 1
        options = [e for e in graph.adjacency.get(current, []) if e.traverse(current) is not None]
        if traversal:
            last_key = traversal[-1][0].edge_key
            non_backtrack = [e for e in options if e.edge_key != last_key]
            if non_backtrack:
                options = non_backtrack
        if not options:
            return None
        rng.shuffle(options)
        chosen = rng.choice(options[:BEAM_WIDTH])
        step = chosen.traverse(current)
        assert step is not None
        nxt, _segs = step
        traversal.append((chosen, current))
        dist_so_far += chosen.length_m
        current = nxt
        if current == start_node:
            return traversal
        remaining = target_m - dist_so_far
        current_info = graph.nodes[current]
        straight_line_m = routes_spatial.equirect_distance_m(current_info.lat, current_info.lng, start_info.lat, start_info.lng)
        if remaining > straight_line_m * HOME_ESTIMATE_INFLATION:
            continue  # not plausibly close enough yet to be worth an exact check
        dist_map, prev_edge, prev_node = _dijkstra(graph, current, target=start_node)
        home_dist = dist_map.get(start_node)
        if home_dist is None:
            continue
        if remaining <= home_dist * TURN_HOME_SLACK:
            traversal.extend(_forward(_reconstruct(start_node, prev_edge, prev_node)))
            return traversal
    return None


def _pick_turnaround(dist_map: dict[int, float], start_node: int, target_dist: float, rng: random.Random) -> int | None:
    tolerance = target_dist * TURNAROUND_TOLERANCE
    candidates = [n for n, d in dist_map.items() if n != start_node and abs(d - target_dist) <= tolerance]
    if candidates:
        return rng.choice(candidates)
    reachable = [n for n in dist_map if n != start_node]
    if not reachable:
        return None
    return min(reachable, key=lambda n: abs(dist_map[n] - target_dist))


Steps = list[tuple[GraphEdge, int, int]]  # (edge, from_node, to_node) in travel order


def _forward(steps: Steps) -> Traversal:
    return [(edge, frm) for edge, frm, _to in steps]


def _way_home(graph: SearchGraph, frm: int, start_node: int) -> Traversal | None:
    """The shortest way from `frm` to the start in the direction of travel;
    retracing the way out fails across a one-way edge."""
    dist_map, prev_edge, prev_node = _dijkstra(graph, frm, target=start_node)
    return _forward(_reconstruct(start_node, prev_edge, prev_node)) if start_node in dist_map else None


def _outbound_leg(
    ctx: _SearchContext, start_node: int, outbound_target: float, rng: random.Random
) -> tuple[Steps, float] | None:
    """The steps to a turnaround node near outbound_target, and its distance."""
    dist_map, prev_edge, prev_node = ctx.tree(start_node, outbound_target)
    turnaround = _pick_turnaround(dist_map, start_node, outbound_target, rng)
    if turnaround is None:
        return None
    return _reconstruct(turnaround, prev_edge, prev_node), dist_map[turnaround]


def _generate_out_and_back(ctx: _SearchContext, start_node: int, target_m: float, rng: random.Random) -> Traversal | None:
    leg = _outbound_leg(ctx, start_node, target_m / 2.0, rng)
    if leg is None:
        return None
    out_steps, _dist = leg
    back = _way_home(ctx.graph, out_steps[-1][2], start_node)
    return None if back is None else _forward(out_steps) + back


def _generate_lollipop(ctx: _SearchContext, start_node: int, target_m: float, rng: random.Random) -> Traversal | None:
    leg = _outbound_leg(ctx, start_node, target_m * LOLLIPOP_STEM_FRACTION, rng)
    if leg is None:
        return None
    stem_steps, stem_dist = leg
    loop_steps = _generate_loop(ctx, stem_steps[-1][2], max(target_m - 2 * stem_dist, 0.0), rng)
    if loop_steps is None:
        return None
    back = _way_home(ctx.graph, stem_steps[-1][2], start_node)
    return None if back is None else _forward(stem_steps) + loop_steps + back


_GENERATORS = {"loop": _generate_loop, "out_and_back": _generate_out_and_back, "lollipop": _generate_lollipop}


def _flatten(traversal: Traversal) -> list[tuple[int, bool]]:
    segments: list[tuple[int, bool]] = []
    for edge, frm in traversal:
        step = edge.traverse(frm)
        assert step is not None
        segments.extend(step[1])
    return segments


def _geometry(edge_rows: Mapping[int, dict], segments: Sequence[tuple[int, bool]]) -> list[tuple[float, float, float | None]]:
    points: list[tuple[float, float, float | None]] = []
    for edge_id, forward in segments:
        row = edge_rows.get(edge_id)
        if row is None:
            continue
        pts = network.unpack_edge_points(bytes(row["points_blob"]))
        seq = pts if forward else list(reversed(pts))
        for lat, lng, _d, ele in seq:
            if points and points[-1][0] == lat and points[-1][1] == lng:
                continue
            points.append((lat, lng, ele))
    return points


def _ways(edge_rows: Mapping[int, dict], segments: Sequence[tuple[int, bool]]) -> list[dict]:
    """The route's ways in travel order, consecutive edges of one way merged."""
    ways: list[dict] = []
    for edge_id, _forward in segments:
        row = edge_rows.get(edge_id)
        if row is None:
            continue
        if ways and ways[-1]["way_id"] == row["way_id"]:
            ways[-1]["length_m"] += row["length_m"]
            continue
        ways.append({
            "way_id": row["way_id"],
            "name": row["way_name"],
            "surface_class": row["surface_class"],
            "length_m": row["length_m"],
        })
    return ways


# ---------------------------------------------------------------------------
# Constraint checking
# ---------------------------------------------------------------------------


def _runnable_pct(score: dict) -> float:
    """"Runnable" for filtering purposes counts both comfortably-runnable
    and grind (runnable but slow) distance -- only "hike" is excluded."""
    return cast(float, score["runnability"]["run_pct"]) + cast(float, score["runnability"]["grind_pct"])


def _soft_misses(
    score: dict, request: RouteSearchRequest, touched_way_ids: set[int],
    purpose_gain_ft_per_mi: tuple[float | None, float | None],
) -> list[ConstraintMiss]:
    misses: list[ConstraintMiss] = []
    dmin, dmax = request["distance_m_min"], request["distance_m_max"]
    distance_m = cast(float, score["distance_m"])
    if not (dmin <= distance_m <= dmax):
        misses.append({"constraint": "distance_range", "detail": f"{distance_m:.0f}m outside [{dmin:.0f}, {dmax:.0f}]"})

    min_trail = request.get("min_trail_m")
    if min_trail is not None and cast(float, score["trail_distance_m"]) < min_trail:
        misses.append({"constraint": "min_trail_m", "detail": f"{score['trail_distance_m']:.0f}m trail, need {min_trail:.0f}m"})

    min_runnable = request.get("min_runnable_pct")
    if min_runnable is not None:
        runnable_pct = _runnable_pct(score)
        if runnable_pct < min_runnable:
            misses.append({"constraint": "min_runnable_pct", "detail": f"{runnable_pct:.0f}% runnable, need {min_runnable:.0f}%"})

    gain_min, gain_max = request.get("gain_m_min"), request.get("gain_m_max")
    if gain_min is None and gain_max is None and score["has_elevation"]:
        # The purpose's climb band, which is per mile, over this route's distance.
        miles = cast(float, score["distance_m"]) / MILE_M
        lo, hi = purpose_gain_ft_per_mi
        gain_min = None if lo is None else lo * miles / FT_PER_M
        gain_max = None if hi is None else hi * miles / FT_PER_M
    if gain_min is not None or gain_max is not None:
        if score["gain_m"] is None:
            misses.append({"constraint": "gain_range", "detail": "no elevation data"})
        elif not score["has_elevation"]:
            misses.append({
                "constraint": "gain_range",
                "detail": f"elevation covers only {score['elevation_coverage_pct']:.0f}% of the route",
            })
        else:
            gain_m = cast(float, score["gain_m"])
            if gain_min is not None and gain_m < gain_min:
                misses.append({"constraint": "gain_range", "detail": f"{gain_m:.0f}m gain, need >= {gain_min:.0f}m"})
            if gain_max is not None and gain_m > gain_max:
                misses.append({"constraint": "gain_range", "detail": f"{gain_m:.0f}m gain, need <= {gain_max:.0f}m"})

    include = request.get("include_way_ids")
    if include:
        missing = set(include) - touched_way_ids
        if missing:
            misses.append({"constraint": "include_way_ids", "detail": f"missing way ids {sorted(missing)}"})
    return misses


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    shape: str
    start: Start
    segments: list[tuple[int, bool]]
    score: dict
    missed: list[ConstraintMiss]
    built_from: str = "walk"
    # The terms of assemble.py's score.
    ranking: dict | None = None
    stops: list[dict] = field(default_factory=list)


def _quality(candidate: _Candidate, target_m: float) -> float:
    closeness = 1.0 - min(abs(cast(float, candidate.score["distance_m"]) - target_m) / max(target_m, 1.0), 1.0)
    return closeness + _runnable_pct(candidate.score) / 100.0


def _cover(edge_rows: Mapping[int, dict], segments: Sequence[tuple[int, bool]]) -> dict[int, float]:
    """The meters a route runs on each original edge: a piece cut from an
    edge at a start, a stop or a turning point counts toward the edge it came from."""
    cover: defaultdict[int, float] = defaultdict(float)
    for eid, _ in segments:
        row = edge_rows[eid]
        cover[row.get("origin", eid)] += row["length_m"]
    return cover


def _duplicate(a: Mapping[int, float], b: Mapping[int, float]) -> bool:
    shared_m = sum(min(m, b[k]) for k, m in a.items() if k in b)
    return shared_m >= DUPLICATE_SHARE * max(sum(a.values()), sum(b.values()), 1.0)


class _Footprint(NamedTuple):
    cells: frozenset[tuple[int, int]]
    # The cells and every neighbor of them: a point within a cell's width of
    # the route lies in it.
    halo: frozenset[tuple[int, int]]


@lru_cache(maxsize=16384)
def _edge_footprint(points_blob: bytes) -> _Footprint:
    cells = {
        (int(lat * routes_spatial.METERS_PER_DEG_LAT // FOOTPRINT_CELL_M), int(lng * routes_spatial.meters_per_deg_lng(lat) // FOOTPRINT_CELL_M))
        for lat, lng, _d, _e in network.unpack_edge_points(points_blob)
    }
    return _Footprint(frozenset(cells), frozenset((x + dx, y + dy) for x, y in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)))


def _footprint(edge_rows: Mapping[int, dict], segments: Sequence[tuple[int, bool]]) -> _Footprint:
    """The ground a route covers, as the grid cells its points fall in."""
    parts = [_edge_footprint(bytes(edge_rows[eid]["points_blob"])) for eid, _ in segments]
    return _Footprint(frozenset().union(*(p.cells for p in parts)), frozenset().union(*(p.halo for p in parts)))


def _overlap(a: _Footprint, b: _Footprint) -> float:
    """The share of the smaller route's ground that the other covers, whatever
    ways each runs on."""
    small, big = (a, b) if len(a.cells) <= len(b.cells) else (b, a)
    return len(small.cells & big.halo) / max(len(small.cells), 1)


def _diversify(candidates: list[_Candidate], edge_rows: Mapping[int, dict], target_m: float, n: int) -> list[_Candidate]:
    """Up to n of `candidates`, best first, each penalized for the ground it
    shares with those already selected. A section-built candidate ranks by
    its score scaled to the best in the pool, so the penalty weighs the same
    as for a walk's quality, and one under DIVERSITY_FLOOR of the best waits
    for the rest. A candidate sharing more than SIMILAR_SHARE of its ground with
    a selected one is never returned, so fewer than n come back when the rest
    are alike."""
    remaining = list(candidates)
    footprints = {id(c): _footprint(edge_rows, c.segments) for c in candidates}
    selected: list[_Candidate] = []
    best_score = max((cast(dict, c.ranking)["score"] for c in candidates if c.ranking is not None), default=0.0)

    def base(c: _Candidate) -> float:
        if c.ranking is None:
            return _quality(c, target_m)
        return c.ranking["score"] / best_score if best_score > 0 else c.ranking["score"]

    while remaining and len(selected) < n:
        def penalized(c: _Candidate) -> float:
            shared = min(sum(_overlap(footprints[id(c)], footprints[id(s)]) for s in selected), 1.0)
            return base(c) - DIVERSITY_WEIGHT * shared

        eligible = [c for c in remaining if c.ranking is None or base(c) >= DIVERSITY_FLOOR] or remaining
        best = max(eligible, key=penalized)
        selected.append(best)
        remaining = [c for c in remaining if c is not best and _overlap(footprints[id(c)], footprints[id(best)]) <= SIMILAR_SHARE]
    return selected


def _identity(edge_rows: Mapping[int, dict], segments: list[tuple[int, bool]]) -> frozenset[int]:
    """The original edges a route runs, in either direction."""
    return frozenset(edge_rows[eid].get("origin", eid) for eid, _ in segments)


def _default_seed(request: RouteSearchRequest) -> int:
    """A request with no explicit seed still reproduces: a hash of its
    parameters (Python's string hash is randomized per process)."""
    canonical = json.dumps(request, sort_keys=True, default=str)
    return int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:8], "big")


def resolve_starts(
    conn: sqlite3.Connection,
    area_id: int,
    *,
    starts: Sequence[Start] | None,
    origin: tuple[float, float] | None,
    max_drive_minutes: float | None,
    drive_k: int,
) -> list[Start]:
    """An explicit starts list, a single start at origin, or (with
    max_drive_minutes) the drive-to-trailhead set around origin, which
    includes origin itself."""
    if max_drive_minutes is not None:
        if starts:
            raise ValueError("max_drive_minutes and starts are mutually exclusive")
        if origin is None:
            raise ValueError(f"max_drive_minutes needs start_lat/start_lng as the drive origin")
        return cast(list[Start], drive_starts.drive_starts(conn, area_id, origin[0], origin[1], max_drive_minutes, k=drive_k))
    if starts:
        return list(starts)
    if origin is not None:
        return [{"lat": origin[0], "lng": origin[1]}]
    raise ValueError(f"find_routes needs start_lat/start_lng or a starts list")


def require_searchable(conn: sqlite3.Connection, area_id: int) -> None:
    """Raise ValueError unless the area's current network can be searched; an
    outdated one starts rebuilding."""
    area_status = conn.execute("SELECT status FROM areas WHERE area_id = ?", [area_id]).fetchone()
    if area_status is not None and area_status["status"] != "ready":
        # A failed or interrupted refresh leaves the committed graph searchable.
        has_graph = conn.execute("SELECT 1 FROM net_graphs WHERE area_id = ?", [area_id]).fetchone()
        if has_graph is None:
            raise ValueError(
                f"area {area_id} is not ready (status: {area_status['status']}); "
                "check get_area_status and search again when it is ready"
            )

    if areas.rebuild_stale_network(conn, area_id):
        raise ValueError(
            f"area {area_id} is being rebuilt for a newer network model; "
            "check get_area_status and search again when it is ready"
        )


@contextmanager
def search_slot() -> Iterator[None]:
    """Hold the process-wide search lock, for work that competes with a
    search for CPU (a survey's shortest-path pass)."""
    with _search_lock:
        yield


def search_lock_held() -> bool:
    return _search_lock.locked()


def load_search_graph(conn: sqlite3.Connection, area_id: int) -> tuple[SearchGraph, dict[int, dict]]:
    """The area's cached graph and edge rows, loading them if needed. Load
    before search_slot: a load must never wait on, or be waited on by, the
    search lock."""
    return _cached_base_graph(conn, area_id)


def start_graph(
    loaded: tuple[SearchGraph, dict[int, dict]], lat: float, lng: float, radius_m: float, remove_way_ids: set[int]
) -> tuple[SearchGraph, MutableMapping[int, dict], int | None]:
    """A request-local copy of a load_search_graph result without
    remove_way_ids' edges and with nothing farther than radius_m from
    (lat, lng), plus that point spliced in. Returns the graph, its edge rows
    (the shared cache layered under the splice's own), and the start's node
    id (None when no edge lies within START_SNAP_RADIUS_M). Call it inside
    search_slot."""
    base_graph, cached_edge_rows = loaded
    graph = _copy_graph(base_graph)
    _remove_ways(graph, cached_edge_rows, remove_way_ids)
    edge_rows: MutableMapping[int, dict] = ChainMap({}, cached_edge_rows)
    _bound_graph(graph, [(lat, lng)], radius_m)
    ids = itertools.count(-1, -1)
    node_id = _splice_start(graph, edge_rows, _edge_index(edge_rows, graph.edges_by_key), lat, lng, ids)
    return graph, edge_rows, node_id


def _evaluate(
    conn: sqlite3.Connection, area_id: int, request: RouteSearchRequest, segments: list[tuple[int, bool]],
    prefs: dict, runnability: network.RunnabilityFn, edge_rows: MutableMapping[int, dict],
    purpose_gain_ft_per_mi: tuple[float | None, float | None],
) -> tuple[dict, list[ConstraintMiss]] | None:
    """A path's score and the soft constraints it misses, or None when it
    cannot be scored or breaks the road-exposure limit."""
    try:
        score = cast(dict, network.score_path(conn, area_id, segments, prefs, runnability, extra_edges=edge_rows))
    except ValueError:
        return None
    if score["road_exposure"]["violations"]:
        return None
    touched_way_ids = {edge_rows[eid]["way_id"] for eid, _ in segments if eid in edge_rows}
    return score, _soft_misses(score, request, touched_way_ids, purpose_gain_ft_per_mi)


def validate_request(conn: sqlite3.Connection, request: RouteSearchRequest) -> RouteSearchRequest:
    """The request with absent, None, and empty-list parameters dropped (they
    are the same request, seed included), or ValueError when it cannot be
    searched."""
    if not request["starts"]:
        raise ValueError("find_routes needs at least one start")
    request = cast(RouteSearchRequest, {k: v for k, v in request.items() if v not in (None, [])})
    if request["distance_m_min"] > request["distance_m_max"]:
        raise ValueError("distance_m_min must be <= distance_m_max")
    if request.get("n", DEFAULT_N) < 1:
        raise ValueError("n must be at least 1")
    if not 0.0 <= request.get("repeat_value", DEFAULT_REPEAT_VALUE) <= 1.0:
        raise ValueError("repeat_value must be between 0 and 1")
    for stop in request.get("stops") or []:
        at_mi = stop.get("at_mi")
        if at_mi is not None and at_mi <= 0:
            raise ValueError("a stop's at_mi must be positive")
    require_searchable(conn, request["area_id"])
    return request


def find_routes(
    conn: sqlite3.Connection, request: RouteSearchRequest, progress: SearchProgress | None = None
) -> RouteSearchResult:
    """Search synchronously. `progress` lets another thread watch and cancel
    the search; a cancelled search returns what it has found so far."""
    # assemble imports survey, which imports this module.
    from . import assemble

    request = validate_request(conn, request)
    progress = progress or SearchProgress()
    starts_in = request["starts"]
    area_id = request["area_id"]
    shape = request.get("shape", "any")
    n = request.get("n", DEFAULT_N)
    time_budget_s = request.get("time_budget_s", float(os.environ.get(BUDGET_ENV, DEFAULT_TIME_BUDGET_S)))
    if time_budget_s <= 0:
        raise ValueError("time_budget_s must be positive")
    repeat_value = request.get("repeat_value", DEFAULT_REPEAT_VALUE)
    stops_in = request.get("stops") or []
    seed = request.get("seed")
    if seed is None:
        seed = _default_seed(request)
    rng = random.Random(seed)

    prefs = network.get_route_preferences(conn)
    model = terrain.current_model(conn)
    runnability = model.score_profile

    base_graph, cached_edge_rows = _cached_base_graph(conn, area_id)
    built_at = _built_at(conn, area_id)
    # The survey behind the plan takes the search slot itself, so the plan is
    # made before this search holds it.
    plan = assemble.plan_blocks(conn, request, model)
    shapes_to_try: list[str] = [shape] if shape != "any" else ["loop", "out_and_back", "lollipop"]
    warnings = list(plan.warnings)
    with _search_lock:
        progress.started = True
        # Avoided ways leave the graph entirely, so no candidate can touch one.
        avoid_way_ids = (
            set(request.get("avoid_way_ids") or []) | network.avoided_way_ids(conn) | sections.excluded_way_ids(conn)
        )
        space = assemble.connector_space(area_id, built_at, base_graph, cached_edge_rows, avoid_way_ids)
        graph = _copy_graph(space.graph)
        # Layered so splicing's synthetic rows stay request-local, off the shared cache.
        edge_rows: MutableMapping[int, dict] = ChainMap({}, cached_edge_rows)
        target_m = (request["distance_m_min"] + request["distance_m_max"]) / 2.0
        radius_m = request["distance_m_max"] / 2.0
        _bound_graph(
            graph, [(p["lat"], p["lng"]) for p in (*starts_in, *stops_in)], radius_m, plan.keep_nodes(cached_edge_rows)
        )

        ids = itertools.count(-1, -1)
        replaced: dict[int, tuple[int, int, int, int]] = {}
        start_index = _edge_index(edge_rows, graph.edges_by_key)
        start_nodes: list[tuple[int, Start]] = []
        for s in starts_in:
            node_id = _splice_start(graph, edge_rows, start_index, s["lat"], s["lng"], ids, replaced)
            if node_id is not None:
                start_nodes.append((node_id, s))
        if not start_nodes:
            raise ValueError("no network found near any given start")
        stop_nodes: list[tuple[int, Stop]] = []
        for stop in stops_in:
            node_id = _splice_start(graph, edge_rows, start_index, stop["lat"], stop["lng"], ids, replaced)
            if node_id is None:
                raise ValueError(f"no network found near stop {stop.get('label') or (stop['lat'], stop['lng'])}")
            stop_nodes.append((node_id, stop))

        deadline = time.monotonic() + time_budget_s
        qualifying: dict[frozenset[int], _Candidate] = {}
        near_misses: dict[frozenset[int], _Candidate] = {}
        budget_exceeded = False
        progress.snapshot = lambda c: _persist_selected(c, area_id, edge_rows, qualifying, near_misses, target_m, n)

        assembly = assemble.Assembly(
            plan, graph, space, edge_rows, replaced, start_nodes, stop_nodes, prefs, model,
            distance_m_min=request["distance_m_min"], distance_m_max=request["distance_m_max"],
            shapes=shapes_to_try, repeat_value=repeat_value, ids=ids, cancelled=progress.cancelled,
        )
        if plan.blocks:
            pool_target = n * POOL_FACTOR
            # The walk may have to fill in afterwards, so it keeps a share of the budget.
            sections_end = deadline if plan.explicit or stops_in else deadline - (1.0 - SECTIONS_BUDGET_SHARE) * time_budget_s
            for walk in assembly.walks(sections_end):
                if len(qualifying) >= pool_target:
                    break
                segments = _flatten(walk.traversal)
                evaluated = _evaluate(conn, area_id, request, segments, prefs, runnability, edge_rows, plan.gain_band) if segments else None
                if evaluated is None:
                    continue
                score, misses = evaluated
                ranking = assembly.rank(walk, score["gain_m"] if score["has_elevation"] else None)
                candidate = _Candidate(walk.shape, walk.start, segments, score, misses, "sections", ranking, walk.stops)
                identity = _identity(edge_rows, segments)
                pool = near_misses if misses else qualifying
                best = pool.get(identity)
                if best is None or cast(dict, candidate.ranking)["score"] > cast(dict, best.ranking)["score"]:
                    pool[identity] = candidate
                progress.qualifying = len(qualifying)
            budget_exceeded = time.monotonic() > deadline and len(qualifying) < n
        elif stops_in:
            warnings.append("stops can only be passed by routes built from sections, and none lies within reach of a start")
        elif plan.explicit:
            warnings.append("none of the named sections could be built into a route: none lies within reach of a start")
        if plan.explicit and plan.blocks and not qualifying and not near_misses:
            warnings.append(
                "no route could be assembled around the named sections: none could be reached within the approach "
                "limit from a start, or every attempt broke the road-exposure limit"
            )

        # The walk is the fallback for a request no section could serve; it
        # never stands in for sections the request named, and it cannot pass stops.
        walking = (
            not plan.explicit and not stops_in and not qualifying and not budget_exceeded and not progress.cancelled.is_set()
        )
        if walking:
            contract_degree2_chains(graph, ids, protected=[node_id for node_id, _ in start_nodes])
            ctx = _SearchContext(graph=graph)
            attempts = 0
            # Generate well past n so diversification has a pool to choose from.
            pool_target = n * POOL_FACTOR
            attempts_since_new = 0
            while len(qualifying) < pool_target and attempts < MAX_GENERATION_ATTEMPTS:
                if len(qualifying) >= n and attempts_since_new >= STAGNATION_ATTEMPTS:
                    break
                if progress.cancelled.is_set():
                    break
                if time.monotonic() > deadline:
                    budget_exceeded = len(qualifying) < n
                    break
                attempts += 1
                attempts_since_new += 1
                gen_shape = rng.choice(shapes_to_try)
                start_node, start_info = rng.choice(start_nodes)
                traversal = _GENERATORS[gen_shape](ctx, start_node, target_m, rng)
                if not traversal:
                    continue
                segments = _flatten(traversal)
                if not segments:
                    continue
                evaluated = _evaluate(conn, area_id, request, segments, prefs, runnability, edge_rows, plan.gain_band)
                if evaluated is None:
                    continue
                score, misses = evaluated
                ranking = assembly.rank_path(start_info, segments, score["distance_m"], score["gain_m"] if score["has_elevation"] else None)
                candidate = _Candidate(gen_shape, start_info, segments, score, misses, ranking=ranking)
                # The same set of edges walked in either direction is one route.
                identity = _identity(edge_rows, segments)
                pool = near_misses if misses else qualifying
                if identity not in pool:
                    pool[identity] = candidate
                    if not misses:
                        attempts_since_new = 0
                        progress.qualifying = len(qualifying)

    result_candidates = _persist_selected(conn, area_id, edge_rows, qualifying, near_misses, target_m, n)
    conn.commit()
    return {
        "candidates": result_candidates,
        "near_miss": any(c["missed_constraints"] for c in result_candidates),
        "budget_exceeded": budget_exceeded,
        "seed": seed,
        "warnings": warnings,
    }


def _persist_selected(
    conn: sqlite3.Connection,
    area_id: int,
    edge_rows: Mapping[int, dict],
    qualifying: Mapping[frozenset[int], _Candidate],
    near_misses: Mapping[frozenset[int], _Candidate],
    target_m: float,
    n: int,
) -> list[RouteCandidateOut]:
    """The n best of the pool, diversified, persisted; the closest misses when
    nothing qualifies. Safe to call while the search still adds to the pool."""
    if qualifying:
        selected = _diversify(list(qualifying.values()), edge_rows, target_m, n)
    else:
        def near_key(c: _Candidate) -> tuple[int, int, float]:
            return (len(c.missed), c.built_from != "sections", -(c.ranking["score"] if c.ranking else _quality(c, target_m)))

        selected = []
        footprints: list[_Footprint] = []
        for c in sorted(list(near_misses.values()), key=near_key):
            footprint = _footprint(edge_rows, c.segments)
            if len(selected) < n and not any(_overlap(footprint, other) > SIMILAR_SHARE for other in footprints):
                selected.append(c)
                footprints.append(footprint)
    return [_persist_candidate(conn, area_id, edge_rows, c) for c in selected]


def _persist_candidate(
    conn: sqlite3.Connection, area_id: int, edge_rows: Mapping[int, dict], candidate: _Candidate
) -> RouteCandidateOut:
    out: RouteCandidateOut = {
        "candidate_id": "",
        "area_id": area_id,
        "shape": candidate.shape,
        "start": candidate.start,
        "distance_m": candidate.score["distance_m"],
        "score": candidate.score,
        "missed_constraints": candidate.missed,
        "geometry": [list(p) for p in _geometry(edge_rows, candidate.segments)],
        "ways": _ways(edge_rows, candidate.segments),
        "built_from": candidate.built_from,
        "sections_used": cast(list[dict], (candidate.ranking or {}).get("sections_used", [])),
    }
    if candidate.stops:
        out["stops"] = candidate.stops
    if candidate.ranking is not None:
        out["ranking"] = {k: v for k, v in candidate.ranking.items() if k != "sections_used"}
    data = json.dumps({k: v for k, v in out.items() if k != "candidate_id"}, sort_keys=True)
    # The id follows the content, so a route listed mid-search keeps its id in
    # the final list, and persisting it again never shortens a stored expiry.
    out["candidate_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, data))
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=CANDIDATE_TTL_HOURS)).isoformat()
    conn.execute(
        "INSERT INTO net_candidates (candidate_id, expires_at, data) VALUES (?, ?, ?) "
        "ON CONFLICT (candidate_id) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)",
        [out["candidate_id"], expires_at, data],
    )
    return out


# ---------------------------------------------------------------------------
# Candidate retrieval / saving
# ---------------------------------------------------------------------------


def get_route_candidate(conn: sqlite3.Connection, candidate_id: str) -> RouteCandidateOut:
    _purge_expired(conn)
    row = conn.execute("SELECT data FROM net_candidates WHERE candidate_id = ?", [candidate_id]).fetchone()
    if row is None:
        raise CandidateNotFoundError(f"no candidate {candidate_id} (or it expired)")
    return cast(RouteCandidateOut, {"candidate_id": candidate_id, **json.loads(row["data"])})


def save_route_candidate(conn: sqlite3.Connection, candidate_id: str, name: str) -> dict:
    """Upload a GPX built from a candidate's stored geometry as a route
    (source "generated")."""
    candidate = get_route_candidate(conn, candidate_id)
    points = [(cast(float, lat), cast(float, lng), ele) for lat, lng, ele in candidate["geometry"]]
    gpx_bytes = routes.build_gpx(name, points)
    filename = f"generated-{candidate_id}.gpx"
    route_ids = routes.upload_gpx(conn, gpx_bytes, filename, name=name, source="generated")
    return {"route_id": route_ids[0], "candidate_id": candidate_id, "name": name}
