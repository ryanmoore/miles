"""Builders shared across test files. Fixtures live in conftest.py; plain
functions and classes that tests call directly live here (tests/ has no
__init__.py, so test modules import this as `helpers`)."""

import asyncio
import json
import math
import sqlite3
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import osmium
import pytest
import requests
from mcp.server.fastmcp import FastMCP
from osmium.osm.mutable import Node, Relation, Way
from stravalib.exc import Fault

from miles import db, extracts, http_client, routes, routes_spatial


def call_tool(server: FastMCP, name: str, args: dict | None = None):
    """Run one registered MCP tool and decode its JSON text result."""
    content = asyncio.run(server.call_tool(name, args or {}))
    blocks = content[0] if isinstance(content, tuple) else content
    return json.loads(blocks[0].text)


def memory_db() -> sqlite3.Connection:
    """A fresh in-memory database with the full schema. check_same_thread is
    off because request handlers and tool workers run on other threads."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


class FakeResponse:
    """A urlopen-style response. Headers default to a Content-Length matching
    the body; `send` truncates what is actually delivered (a short read)."""

    def __init__(self, body: bytes = b"ok", headers: dict | None = None, *, send: int | None = None) -> None:
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}
        self._body = body[: len(body) if send is None else send]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = len(self._body)
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk


class FakeTokenResponse:
    """The token endpoint's answer: `ok` follows the status, `request` is present
    so a test can check that a Fault built from it carries none."""

    def __init__(self, payload: dict[str, object] | None = None, status: int = 200) -> None:
        self.ok = status == 200
        self.status_code = status
        self.request = object()
        self._payload = payload or {}

    def json(self) -> dict[str, object]:
        return self._payload


class FakeStravaSession:
    """Stands in for the requests session behind the Strava client. The token
    endpoint answers `payload` with `status` and records each form body;
    `token_error` and `api_error` make the token request or any API call raise."""

    def __init__(
        self, payload: dict[str, object] | None = None, *, status: int = 200,
        token_error: Exception | None = None, api_error: Exception | None = None,
    ) -> None:
        self.payload, self.status = payload, status
        self.token_error, self.api_error = token_error, api_error
        self.posts: list[dict[str, str]] = []

    def mount(self, *args: object) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> FakeTokenResponse:
        assert "?" not in url
        if self.token_error is not None:
            raise self.token_error
        self.posts.append(kwargs.get("data"))  # type: ignore[arg-type]
        return FakeTokenResponse(self.payload, self.status)

    def get(self, url: str, **kwargs: object) -> None:
        if self.api_error is not None:
            raise self.api_error

    put = delete = get


class FakeClock:
    """Virtual clock: sleeping advances time, so throttle and backoff tests run
    instantly and can assert on the exact waits."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeClock":
        monkeypatch.setattr(http_client.time, "monotonic", self.monotonic)
        monkeypatch.setattr(http_client.time, "sleep", self.sleep)
        return self


def http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", headers or {}, None)  # type: ignore[arg-type]


def strava_fault(status: int) -> Fault:
    """The exception stravalib raises for an HTTP error answer."""
    response = requests.Response()
    response.status_code = status
    fault = Fault(f"strava answered {status}")
    fault.response = response
    return fault


def urlopen_sequence(monkeypatch: pytest.MonkeyPatch, outcomes: list) -> list:
    """Stub urllib's urlopen: each call returns (or raises) the next outcome,
    repeating the last. Returns the list of requests made."""
    calls: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http_client.urllib.request, "urlopen", fake_urlopen)
    return calls


def insert_activity(conn: sqlite3.Connection, activity_id: int, *, start_date: str | None = None, sport_type: str = "Run", **cols: object) -> None:
    """One plain easy run (a recent date unless given: a fixed old default
    makes weekly-snapshot backfills walk years of weeks); `cols` override or
    add any column of the activities table."""
    values: dict[str, object] = {
        "activity_id": activity_id,
        "name": "Run",
        "sport_type": sport_type,
        "start_date": start_date or f"{date.today() - timedelta(days=3):%Y-%m-%d}T08:00:00",
        "workout_type": 0,
        "run_type": "easy",
        "synced_at": "2026-01-01",
        **cols,
    }
    conn.execute(
        f"INSERT INTO activities ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        list(values.values()),
    )
    conn.commit()


def insert_terrain_segments(conn: sqlite3.Connection, activity_id: int, start_date: str, parts: list[tuple[int, float, float]]) -> None:
    """Stored 20 m terrain segments from (count, speed m/s, grade %) parts."""
    rows, idx = [], 0
    for count, speed, grade in parts:
        for _ in range(count):
            rows.append((activity_id, idx, 20.0 * idx, round(grade * 10), round(speed * 100), 140 + idx // 100))
            idx += 1
    conn.execute("INSERT INTO terrain_segment_dates (activity_id, start_date, segment_count) VALUES (?, ?, ?)", [activity_id, start_date, idx])
    conn.executemany(
        "INSERT INTO terrain_segments (activity_id, idx, dist_m, grade_dpct, speed_cms, heartrate, is_walking, ele_source) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
        rows,
    )


METERS_PER_DEG_LAT = 111194.9
ORIGIN_LAT, ORIGIN_LNG = 40.0, -105.0


def by_deg_lat(meters: float) -> float:
    return meters / METERS_PER_DEG_LAT


def by_deg_lng(lat: float, meters: float) -> float:
    return meters / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)))


def node(node_id: int, lat: float, lon: float, tags: dict | None = None) -> dict:
    """An Overpass-shaped node element."""
    el: dict = {"type": "node", "id": node_id, "lat": lat, "lon": lon}
    if tags:
        el["tags"] = tags
    return el


def way(way_id: int, nodes: list[int], tags: dict) -> dict:
    """An Overpass-shaped way element."""
    return {"type": "way", "id": way_id, "nodes": nodes, "tags": tags}


def box_rings(south: float, west: float, north: float, east: float) -> list:
    """The closed GeoJSON ring of a rectangle, in a list of rings."""
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


def region_polygon(south: float, west: float, north: float, east: float) -> dict:
    return {"type": "Polygon", "coordinates": box_rings(south, west, north, east)}


def region_feature(path: str | None, geometry: dict | None) -> dict:
    """A Geofabrik index feature for the extract at `path` (none when path is
    None) bounded by `geometry`."""
    urls = {"pbf": f"https://download.geofabrik.de/{path}-latest.osm.pbf"} if path else {}
    return {"type": "Feature", "properties": {"id": (path or "none").split("/")[-1], "urls": urls}, "geometry": geometry}


def lerp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    """The point a fraction t of the way from a to b."""
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


class WayBuilder:
    """Accumulates Overpass-shaped nodes and ways, handing out fresh ids.
    `elements` is what build_network takes as a payload."""

    def __init__(self, first_node_id: int = 101, first_way_id: int = 1) -> None:
        self._next_node = first_node_id
        self._next_way = first_way_id
        self.nodes: list[dict] = []
        self.ways: list[dict] = []

    @property
    def elements(self) -> list[dict]:
        return [*self.nodes, *self.ways]

    def node(self, lat: float, lng: float, tags: dict | None = None, node_id: int | None = None) -> int:
        if node_id is None:
            node_id, self._next_node = self._next_node, self._next_node + 1
        self.nodes.append(node(node_id, lat, lng, tags))
        return node_id

    def way(self, node_ids: list[int], tags: dict) -> int:
        way_id, self._next_way = self._next_way, self._next_way + 1
        self.ways.append(way(way_id, node_ids, tags))
        return way_id

    def path(
        self,
        a_id: int,
        a_ll: tuple[float, float],
        b_id: int,
        b_ll: tuple[float, float],
        *,
        highway: str,
        tags: dict | None = None,
        pieces: int = 1,
        interior: int = 0,
    ) -> int:
        """A straight run from node a to node b as `pieces` ways joined at
        fresh nodes (each junction splits the network into its own edge), each
        shaped by `interior` points that belong to no other way and so add
        geometry without adding a junction. Returns the first way's id."""
        way_tags = dict(tags or {}, highway=highway)
        prev_id, first_way = a_id, None
        for i in range(1, pieces + 1):
            end_id = b_id if i == pieces else self.node(*lerp(a_ll, b_ll, i / pieces))
            piece_from, piece_to = lerp(a_ll, b_ll, (i - 1) / pieces), lerp(a_ll, b_ll, i / pieces)
            shape = [self.node(*lerp(piece_from, piece_to, k / (interior + 1))) for k in range(1, interior + 1)]
            way_id = self.way([prev_id, *shape, end_id], way_tags)
            first_way = way_id if first_way is None else first_way
            prev_id = end_id
        assert first_way is not None
        return first_way


class ImmediateThread:
    """Stand-in for threading.Thread that runs its target synchronously in the
    caller's thread, so a background job can be tested without concurrency."""

    def __init__(self, target, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


class FakeSampler:
    """An elevation sampler that never touches a tile cache or the network.
    `elevation_at(index, lat, lng)` gives each point's reading, None for no
    coverage (100 m everywhere by default). Every call is recorded."""

    def __init__(self, elevation_at=None) -> None:
        self._elevation_at = elevation_at or (lambda i, lat, lng: 100.0)
        self.calls: list[list[tuple[float, float]]] = []

    @property
    def points(self) -> list[tuple[float, float]]:
        return [p for call in self.calls for p in call]

    def sample(self, points):
        self.calls.append(list(points))
        return [self._elevation_at(i, lat, lng) for i, (lat, lng) in enumerate(points)]


def none_sampler() -> FakeSampler:
    """No coverage anywhere: every edge reads as having no elevation."""
    return FakeSampler(lambda i, lat, lng: None)


def flat_sampler() -> FakeSampler:
    """100 m everywhere."""
    return FakeSampler()


def climbing_sampler(step_m: float = 3.0, base: float = 100.0) -> FakeSampler:
    """Each sampled sequence climbs steadily from its first point."""
    return FakeSampler(lambda i, lat, lng: base + i * step_m)


def latitude_sampler(base: float = 100.0, no_coverage: frozenset[int] = frozenset()) -> FakeSampler:
    """Elevation rises with latitude, so a draped track always has gain;
    the points at the given indices have no coverage."""
    return FakeSampler(lambda i, lat, lng: None if i in no_coverage else base + (lat - 40.0) * 10000.0)


def parse_osrm_coords(url: str) -> list[tuple[float, float]]:
    """The (lat, lng) pairs of an OSRM table URL, origin first."""
    coords_part = url.split("/driving/", 1)[1].split("?", 1)[0]
    out = []
    for pair in coords_part.split(";"):
        lng_s, lat_s = pair.split(",")
        out.append((float(lat_s), float(lng_s)))
    return out


class FakeOsrm:
    """Stands in for the OSRM request drive_starts makes through http_client. The answer is a table
    row whose durations are straight-line kilometres times `seconds_per_km`
    (90 by default, slower than the module's prefilter ceiling, so a candidate
    can pass the prefilter and still miss the budget). `fail_with` raises,
    `body` replaces the answer, `unreachable` answers null for every
    destination, and `delay_s` blocks the call."""

    def __init__(self, seconds_per_km: float = 90.0) -> None:
        self.calls: list[str] = []
        self.seconds_per_km = seconds_per_km
        self.fail_with: Exception | None = None
        self.body: bytes | None = None
        self.unreachable = False
        self.delay_s = 0.0

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail_with is not None:
            raise self.fail_with
        if self.body is not None:
            return self.body
        coords = parse_osrm_coords(url)
        origin, destinations = coords[0], coords[1:]
        if self.unreachable:
            durations: list[float | None] = [0.0, *([None] * len(destinations))]
        else:
            durations = [0.0] + [
                routes_spatial.equirect_distance_m(*origin, *dest) / 1000.0 * self.seconds_per_km for dest in destinations
            ]
        return json.dumps({"code": "Ok", "durations": [durations]}).encode()

    def requested(self, lat: float, lng: float) -> bool:
        """Whether any request so far named this point as a destination."""
        wanted = f"{lng:.6f},{lat:.6f}"
        return any(wanted in url.split("/driving/", 1)[1].split("?", 1)[0].split(";")[1:] for url in self.calls)


TA1_LAT = ORIGIN_LAT + by_deg_lat(300.0)  # trailhead ~300m from the origin
TA2_LAT = TA1_LAT + by_deg_lat(200.0)  # the trail runs 200m north from there
# Parking sits past the trail's far end, >300m from the trailhead itself (so
# trailhead/parking dedupe doesn't collapse them) but within 150m of the trail
# (so it still qualifies as a candidate).
P1_LAT = TA2_LAT + by_deg_lat(140.0)  # unnamed parking near the trail's far end
P2_LAT = TA2_LAT + by_deg_lat(145.0)  # a second parking 5m from P1: a dedupe candidate
P3_LAT = TA2_LAT + by_deg_lat(142.0)  # private parking, also near the trail
P4_LAT = TA1_LAT + by_deg_lat(5000.0)  # parking 5km from the nearest trail
TH2_LAT = ORIGIN_LAT + by_deg_lat(55000.0)  # trailhead 55km away: the prefilter drops it
TH3_LAT = ORIGIN_LAT + by_deg_lat(9000.0)  # trailhead 9km away: passes the prefilter, fails a 10 minute budget at 90 s/km
TH1_ID, P1_ID, P2_ID, P3_ID, P4_ID, TH2_ID, TH3_ID = 110, 111, 112, 113, 114, 210, 310


def meadow_payload() -> dict:
    """A 200 m trail with a trailhead at its south end and parking beyond its
    north end, plus decoys: private parking, parking far from any trail, and
    trailheads too far or too slow to drive to from the origin."""
    return {
        "elements": [
            node(101, TA1_LAT, ORIGIN_LNG), node(102, TA2_LAT, ORIGIN_LNG),
            way(1001, [101, 102], {"highway": "footway", "name": "Meadow Trail"}),
            node(TH1_ID, TA1_LAT, ORIGIN_LNG, {"highway": "trailhead", "name": "Meadow Trailhead"}),
            node(P1_ID, P1_LAT, ORIGIN_LNG, {"amenity": "parking"}),
            node(P2_ID, P2_LAT, ORIGIN_LNG, {"amenity": "parking"}),
            node(P3_ID, P3_LAT, ORIGIN_LNG, {"amenity": "parking", "access": "private"}),
            node(P4_ID, P4_LAT, ORIGIN_LNG, {"amenity": "parking"}),
            node(TH2_ID, TH2_LAT, ORIGIN_LNG, {"highway": "trailhead", "name": "Far Trailhead"}),
            node(TH3_ID, TH3_LAT, ORIGIN_LNG, {"highway": "trailhead", "name": "Budget Trailhead"}),
        ]
    }


def write_pbf(
    path: Path,
    nodes: list[tuple[int, float, float, dict]],
    ways: list[tuple[int, list[int], dict]],
    relations: list[tuple[int, list[tuple[str, int, str]], dict]] | None = None,
) -> None:
    """A tiny synthetic .osm.pbf: nodes as (id, lat, lon, tags), ways as
    (id, node_ids, tags), relations as (id, [(member type, ref, role)], tags).
    No bbox header, unlike a real Geofabrik extract."""
    writer = osmium.SimpleWriter(str(path))
    for node_id, lat, lon, tags in nodes:
        writer.add_node(Node(id=node_id, location=(lon, lat), tags=tags, version=1))
    for way_id, node_ids, tags in ways:
        writer.add_way(Way(id=way_id, nodes=node_ids, tags=tags, version=1))
    for relation_id, members, tags in relations or []:
        writer.add_relation(Relation(id=relation_id, members=members, tags=tags, version=1))
    writer.close()


def fake_extract_record(region: str = "test/region") -> extracts.ExtractRecord:
    """A registered-extract row for tests that stub out the file itself."""
    return {
        "region": region, "path": "/nonexistent.osm.pbf", "sha256": "x",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "checked_at": datetime.now(timezone.utc).isoformat(), "data_as_of": datetime.now(timezone.utc).isoformat(),
        "etag": None, "last_modified": None,
    }


def trkpt(lat: float, lng: float, ele: float | None = None) -> str:
    if ele is None:
        return f'<trkpt lat="{lat}" lon="{lng}"/>'
    return f'<trkpt lat="{lat}" lon="{lng}"><ele>{ele}</ele></trkpt>'


def make_gpx(points: list[tuple[float, float, float | None]], name: str = "Track", creator: str = "Test") -> bytes:
    """One-track GPX 1.1 file; a None elevation leaves out the <ele> element."""
    body = f"<trk><name>{name}</name><trkseg>{''.join(trkpt(*p) for p in points)}</trkseg></trk>"
    return (
        '<?xml version="1.0"?>\n'
        f'<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>'
    ).encode("utf-8")


def line_points(n: int = 40, lat0: float = 40.0, lng0: float = -105.0, step_deg: float = 0.0002, ele0: float | None = 100.0, gain_per_point: float = 2.0) -> list[tuple[float, float, float | None]]:
    """A straight north-running line of n points, climbing gain_per_point
    each (no elevation at all when ele0 is None)."""
    return [(lat0 + i * step_deg, lng0, None if ele0 is None else ele0 + gain_per_point * i) for i in range(n)]


def upload_line_route(
    conn: sqlite3.Connection, lat0: float, lng0: float, n: int = 60, step_deg: float = 0.0002, name: str = "R", ele0: float | None = 100.0
) -> int:
    """Upload a straight north-running track climbing one meter per point and
    return its route id."""
    points = line_points(n, lat0=lat0, lng0=lng0, step_deg=step_deg, ele0=ele0, gain_per_point=1.0)
    return routes.upload_gpx(conn, make_gpx(points, name=name), f"{name}.gpx", name=name)[0]


def activity_row(activity_id: int, start: str = "2026-01-05T08:00:00", *, workout_type: int = 0, sport: str = "Run", **fields: object) -> db.ActivityRow:
    """A syncable activity as the Strava mapper would produce it."""
    row: db.ActivityRow = {
        "activity_id": activity_id, "name": f"Run {activity_id}", "sport_type": sport, "start_date": start,
        "workout_type": workout_type, "run_type": db.WORKOUT_TYPE_MAP.get(workout_type, "easy"),
        "distance_m": 8000.0, "moving_time_s": 2400, "elapsed_time_s": 2400, "total_elevation_gain_m": 50.0,
        "average_speed_mps": 3.3, "max_speed_mps": 4.0, "average_heartrate": 140.0, "max_heartrate": 160.0,
        "average_cadence": 85.0, "gear_id": None, "strava_url": "", "synced_at": "2026-01-01T00:00:00+00:00",
        "start_lat": None, "start_lng": None, "raw_json": "{}",
    }
    row.update(fields)  # type: ignore[typeddict-item]
    return row
