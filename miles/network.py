"""Trail/road network graph built from raw map tile payloads, plus the
preference profile, way overrides, and path scoring that read it.

Nodes are OSM nodes shared by two or more routable ways, way endpoints, and
amenity nodes snapped onto their nearest edge by splitting it, so an amenity
is a real traversable node. Edges are way pieces between nodes, resampled to
gpx_parse's 20m spacing and draped with an injected elevation sampler. Edge
elevation stays unsmoothed: a window chosen from roughness needs a profile
longer than most edges, so smoothing happens over whole paths (score_path),
exactly as it does for a stored route.

Road comfort is not stored on an edge: only its static inputs are, and the
tier is computed from the current preference profile at score time, so a
preference edit changes future scores with no graph rebuild.

Rebuilt wholesale per area, gated by NETWORK_DERIVE_VERSION; the raw tile
payloads (owned by area import) are the ground truth, not these tables. The
area's sections (sections.py) are rebuilt in the same transaction.
"""

import copy
import itertools
import json
import math
import re
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Hashable, Iterable, Mapping, MutableMapping, Sequence, cast
from typing_extensions import TypedDict

from . import gpx_parse, routes_spatial
from .elevation import ElevationSampler

if TYPE_CHECKING:
    from . import sections

NETWORK_DERIVE_VERSION = 11

# Paths with no vehicle traffic: they carry no road-comfort tier.
TRAIL_HIGHWAYS = frozenset({"path", "footway", "bridleway", "steps", "cycleway"})

# Drivable classes kept in the routing graph, each with a road-comfort tier.
ROAD_LIKE_HIGHWAYS = frozenset(
    {"secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
     "residential", "living_street", "service", "road", "track"}
)

# Streets a footway can be the sidewalk of. Tracks are out (a trail beside a
# fire road is not a sidewalk), motorways are out (no sidewalks), and the
# routing-excluded primary/trunk roads are in: they're where sidewalks are.
SIDEWALK_ROAD_HIGHWAYS = (ROAD_LIKE_HIGHWAYS - {"track"}) | {"primary", "primary_link", "trunk", "trunk_link"}

PAVED_SURFACE_VALUES = frozenset(
    {"paved", "asphalt", "concrete", "concrete:plates", "concrete:lanes",
     "paving_stones", "sett", "cobblestone", "metal", "wood"}
)
UNPAVED_SURFACE_VALUES = frozenset(
    {"unpaved", "gravel", "dirt", "ground", "grass", "sand", "compacted",
     "fine_gravel", "earth", "mud", "woodchips", "pebblestone"}
)

# A shoulder is paint beside moving traffic, not a separated path, so it is
# deliberately absent.
_SEPARATION_TAG_KEYS = (
    "sidewalk", "sidewalk:left", "sidewalk:right", "sidewalk:both",
    "cycleway", "cycleway:left", "cycleway:right", "cycleway:both",
)
_SEPARATION_VALUES = frozenset({"yes", "both", "left", "right", "separate", "track"})

DEFAULT_MAXSPEED_MPH_BY_CLASS: dict[str, float] = {
    "secondary": 35.0, "secondary_link": 30.0,
    "tertiary": 25.0, "tertiary_link": 25.0,
    "unclassified": 25.0, "residential": 25.0, "living_street": 15.0,
    "service": 15.0, "track": 15.0, "road": 25.0,
}

AMENITY_SNAP_RADIUS_M = 300.0


RunnabilityResult = dict[str, float]
RunnabilityFn = Callable[[Sequence[float]], RunnabilityResult]

# One edge traversal: (edge_id, forward). forward=False walks the edge from
# its to_node back to its from_node.
EdgeSequenceEntry = tuple[int, bool]


class NetworkStats(TypedDict):
    area_id: int
    node_count: int
    edge_count: int
    amenity_count: int
    excluded_way_count: int


class RoadExposureViolation(TypedDict):
    start_m: float
    end_m: float
    length_m: float


class RoadExposure(TypedDict):
    max_continuous_uncomfortable_m: float
    violations: list[RoadExposureViolation]
    totals_m: dict[str, float]
    avoided_ways: list[int]


class PathScore(TypedDict):
    distance_m: float
    trail_distance_m: float
    # The part of trail_distance_m split by kind: footpath-width singletrack
    # and road-width tracks (a highway=track is trail here, not unpaved road).
    singletrack_distance_m: float
    track_distance_m: float
    unpaved_road_distance_m: float
    paved_distance_m: float
    # Access spurs to a trailhead or parking; counted in neither trail, unpaved
    # nor paved distance.
    connector_distance_m: float
    # gain_m/loss_m cover only the stretches of consecutive edges that have
    # elevation, each smoothed as a route would be; has_elevation is True
    # only when every edge has it, elevation_coverage_pct says how much of
    # the distance they cover.
    gain_m: float | None
    loss_m: float | None
    has_elevation: bool
    elevation_coverage_pct: float
    runnability: RunnabilityResult
    road_exposure: RoadExposure


DEFAULT_PREFERENCES: dict = {
    "road_comfort": {
        "comfortable_max_mph": 35.0,
        "uncomfortable_min_mph": 40.0,
        "comfortable_paved_classes": ["residential", "unclassified", "tertiary", "living_street"],
        "through_traffic_classes": ["secondary", "secondary_link", "tertiary_link"],
    },
    "max_continuous_uncomfortable_m": 805.0,  # 0.5 mi
}


# ---------------------------------------------------------------------------
# Schema / connection
# ---------------------------------------------------------------------------


def graph_is_stale(conn: sqlite3.Connection, area_id: int) -> bool:
    """True when the area has a built graph from an older NETWORK_DERIVE_VERSION.
    An area with no graph at all is not stale, just not built."""
    row = conn.execute("SELECT derive_version FROM net_graphs WHERE area_id = ?", [area_id]).fetchone()
    return row is not None and row["derive_version"] != NETWORK_DERIVE_VERSION


def init_network_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_graphs (
            area_id            INTEGER PRIMARY KEY,
            derive_version     INTEGER NOT NULL,
            built_at           TEXT NOT NULL,
            node_count         INTEGER NOT NULL,
            edge_count         INTEGER NOT NULL,
            excluded_way_count INTEGER NOT NULL
        )
    """)
    # kind: 'junction' | 'endpoint' | 'trailhead' | 'parking' | 'drinking_water' |
    # 'toilets' | 'start' (a search's query-time start; in-memory graphs only).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_nodes (
            node_id         INTEGER PRIMARY KEY,
            area_id         INTEGER NOT NULL,
            osm_node_id     INTEGER NOT NULL,
            kind            TEXT NOT NULL,
            name            TEXT,
            lat             REAL NOT NULL,
            lng             REAL NOT NULL,
            UNIQUE(area_id, osm_node_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_nodes_area ON net_nodes(area_id)")
    # points_blob: packed (lat, lng, dist_m, ele_m) doubles at 20m spacing,
    # from_node -> to_node only; ele_m is the unsmoothed terrain-model value,
    # NaN without elevation. The backward profile is derived at scoring time.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_edges (
            edge_id                 INTEGER PRIMARY KEY,
            area_id                 INTEGER NOT NULL,
            way_id                  INTEGER NOT NULL,
            way_name                TEXT,
            highway                 TEXT NOT NULL,
            surface_class           TEXT NOT NULL,
            oneway                  TEXT NOT NULL DEFAULT 'no',
            from_node_id            INTEGER NOT NULL REFERENCES net_nodes(node_id) ON DELETE CASCADE,
            to_node_id              INTEGER NOT NULL REFERENCES net_nodes(node_id) ON DELETE CASCADE,
            length_m                REAL NOT NULL,
            has_elevation           INTEGER NOT NULL,
            maxspeed_mph            REAL,
            maxspeed_filled         INTEGER,
            lanes                   INTEGER,
            has_separation          INTEGER,
            points_blob             BLOB NOT NULL,
            trail_kind              TEXT,
            structure               TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_edges_area ON net_edges(area_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_edges_way ON net_edges(way_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_preferences (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            profile_json TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)
    # Athlete judgment, never derived; keyed by way_id so it survives a rebuild.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_way_overrides (
            way_id     INTEGER PRIMARY KEY,
            verdict    TEXT NOT NULL CHECK (verdict IN ('comfortable', 'avoid')),
            note       TEXT,
            created_at TEXT NOT NULL
        )
    """)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def _merge(base: dict, overrides: dict) -> dict:
    """base with overrides laid over it: an object value is merged one level
    deep into the base's object, anything else replaces it."""
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def get_route_preferences(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT profile_json FROM net_preferences WHERE id = 1").fetchone()
    return _merge(DEFAULT_PREFERENCES, json.loads(row["profile_json"]) if row else {})


def _validate_preferences(prefs: dict) -> None:
    rc = prefs.get("road_comfort")
    if not isinstance(rc, dict):
        raise ValueError("road_comfort must be an object")
    comfortable_max = rc.get("comfortable_max_mph")
    uncomfortable_min = rc.get("uncomfortable_min_mph")
    if not isinstance(comfortable_max, (int, float)) or not isinstance(uncomfortable_min, (int, float)):
        raise ValueError("road_comfort.comfortable_max_mph and uncomfortable_min_mph must be numbers")
    if uncomfortable_min <= comfortable_max:
        raise ValueError("road_comfort.uncomfortable_min_mph must be greater than comfortable_max_mph")
    for key in ("comfortable_paved_classes", "through_traffic_classes"):
        if not isinstance(rc.get(key), list) or not all(isinstance(v, str) for v in rc[key]):
            raise ValueError(f"road_comfort.{key} must be a list of strings")

    max_uncomfortable = prefs.get("max_continuous_uncomfortable_m")
    if not isinstance(max_uncomfortable, (int, float)) or max_uncomfortable <= 0:
        raise ValueError("max_continuous_uncomfortable_m must be a positive number")


def set_route_preferences(conn: sqlite3.Connection, patch: dict) -> dict:
    for key in patch:
        if key not in DEFAULT_PREFERENCES:
            raise ValueError(f"unknown preference key: {key}")
    updated = _merge(get_route_preferences(conn), patch)
    _validate_preferences(updated)
    conn.execute(
        """
        INSERT INTO net_preferences (id, profile_json, updated_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET profile_json = excluded.profile_json, updated_at = excluded.updated_at
        """,
        [json.dumps(updated), datetime.now(timezone.utc).isoformat()],
    )
    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Way overrides
# ---------------------------------------------------------------------------


def set_way_override(conn: sqlite3.Connection, way_id: int, verdict: str, note: str | None = None) -> dict:
    if verdict not in ("comfortable", "avoid"):
        raise ValueError(f"verdict must be 'comfortable' or 'avoid', got {verdict!r}")
    conn.execute(
        """
        INSERT INTO net_way_overrides (way_id, verdict, note, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(way_id) DO UPDATE SET verdict = excluded.verdict, note = excluded.note, created_at = excluded.created_at
        """,
        [way_id, verdict, note, datetime.now(timezone.utc).isoformat()],
    )
    conn.commit()
    return {"way_id": way_id, "verdict": verdict, "note": note}


def get_way_overrides(conn: sqlite3.Connection, way_ids: Sequence[int]) -> dict[int, str]:
    if not way_ids:
        return {}
    placeholders = ",".join("?" for _ in way_ids)
    rows = conn.execute(
        f"SELECT way_id, verdict FROM net_way_overrides WHERE way_id IN ({placeholders})", list(way_ids)
    ).fetchall()
    return {r["way_id"]: r["verdict"] for r in rows}


def avoided_way_ids(conn: sqlite3.Connection) -> set[int]:
    """Every way the athlete has marked 'avoid'."""
    rows = conn.execute("SELECT way_id FROM net_way_overrides WHERE verdict = 'avoid'").fetchall()
    return {r["way_id"] for r in rows}


# ---------------------------------------------------------------------------
# Tag parsing helpers
# ---------------------------------------------------------------------------


def _way_points(way: dict, node_coords: dict[int, tuple[float, float]]) -> list[tuple[float, float]] | None:
    geom = way.get("geometry")
    if geom:
        pts = [(p["lat"], p["lon"]) for p in geom if p is not None]
        return pts if len(pts) >= 2 else None
    node_ids = way.get("nodes") or []
    pts: list[tuple[float, float]] = []
    for nid in node_ids:
        coord = node_coords.get(nid)
        if coord is None:
            return None
        pts.append(coord)
    return pts if len(pts) >= 2 else None


def _way_allowed(highway: str, tags: dict) -> bool:
    if tags.get("access") in ("private", "no"):
        return False
    if tags.get("foot") == "no":
        return False
    if highway == "construction":
        return False
    return True


def _oneway_value(highway: str, tags: dict) -> str:
    """The direction a runner may go: "yes" (from the way's first node on),
    "-1" (against it), or "no". Motor-vehicle roads are two-way for a runner
    unless `oneway:foot` says otherwise; on paths, tracks and the like
    `oneway:foot` wins over `oneway`. `oneway:bicycle` never applies."""
    if "oneway:foot" in tags:
        value = tags["oneway:foot"]
    elif highway in ROAD_LIKE_HIGHWAYS and highway != "track":
        return "no"
    else:
        value = tags.get("oneway")
    if value in ("yes", "true", "1"):
        return "yes"
    if value in ("-1", "reverse"):
        return "-1"
    return "no"


def _surface_class(highway: str, tags: dict) -> str:
    surface = tags.get("surface")
    if highway in TRAIL_HIGHWAYS:
        return "paved" if surface in PAVED_SURFACE_VALUES else "trail"
    if highway == "track":
        # No tracktype value implies a hard surface, so it never upgrades a
        # track past "unpaved road".
        return "paved" if surface in PAVED_SURFACE_VALUES else "unpaved_road"
    return "unpaved_road" if surface in UNPAVED_SURFACE_VALUES else "paved"


TRAIL_KIND_SINGLETRACK = "singletrack"
TRAIL_KIND_TRACK = "track"

# A way at least this wide (meters) is a road-width track, whatever it is
# tagged; one at most TRAIL_NARROW_WIDTH_M is a footpath-width trail.
TRAIL_WIDE_WIDTH_M = 2.5
TRAIL_NARROW_WIDTH_M = 1.5
# Highways that are track-like unless a tag says otherwise.
_TRACK_HIGHWAYS = frozenset({"track", "cycleway"})
# trail_visibility values that describe a path a person has to look for.
_FAINT_VISIBILITY = frozenset({"intermediate", "bad", "horrible", "no"})
# Vehicle access tags that mark a maintenance or farm road.
_VEHICLE_ACCESS_VALUES = frozenset({"yes", "designated", "permissive", "agricultural", "forestry"})
_VEHICLE_ACCESS_KEYS = ("motor_vehicle", "vehicle", "agricultural", "forestry")
# Engineered unpaved surfaces: a path built to this and shared with bicycles
# is a graded multi-use path, not a footpath.
_ENGINEERED_SURFACES = frozenset({"gravel", "fine_gravel", "compacted", "pebblestone"})
_WIDTH_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:m)?\s*$")


def _way_width_m(tags: dict) -> float | None:
    match = _WIDTH_RE.match(tags.get("width") or "")
    return float(match.group(1)) if match else None


def _trail_kind(highway: str, surface_class: str, tags: dict) -> str | None:
    """"singletrack" or "track" for a way that counts as trail, else None.

    Trail is what sections.py counts: unpaved non-sidewalk ways of
    TRAIL_HIGHWAYS, and unpaved tracks. A track is road-width and engineered:
    a wide way, a highway=track or cycleway, one with a tracktype or a
    vehicle-access tag, or a bicycle-designated path on a graded surface.
    Singletrack is the rest, and a footpath-width way or one with a hiking or
    mountain-bike difficulty grade or a faint visibility stays singletrack
    even where it is mapped as a track."""
    if surface_class == "trail":
        pass
    elif not (highway == "track" and surface_class == "unpaved_road"):
        return None
    width = _way_width_m(tags)
    if width is not None and width >= TRAIL_WIDE_WIDTH_M:
        return TRAIL_KIND_TRACK
    if (
        "sac_scale" in tags or "mtb:scale" in tags or tags.get("trail_visibility") in _FAINT_VISIBILITY
        or (width is not None and width <= TRAIL_NARROW_WIDTH_M)
    ):
        return TRAIL_KIND_SINGLETRACK
    if (
        highway in _TRACK_HIGHWAYS or "tracktype" in tags
        or any(tags.get(k) in _VEHICLE_ACCESS_VALUES for k in _VEHICLE_ACCESS_KEYS)
        or (tags.get("bicycle") == "designated" and tags.get("surface") in _ENGINEERED_SURFACES)
    ):
        return TRAIL_KIND_TRACK
    return TRAIL_KIND_SINGLETRACK


# footway covers both trails and street sidewalks/crossings; these values
# on the footway say which.
_SIDEWALK_FOOTWAY_SUBTAGS = frozenset({"sidewalk", "crossing", "traffic_island"})

# A sidewalk sits within about this many meters of its road's centerline:
# wider than a curb offset, too tight to sweep in a trail that merely crosses.
SIDEWALK_ROAD_PROXIMITY_M = 25.0
# Share of an untagged footway's points within SIDEWALK_ROAD_PROXIMITY_M of a
# road-like way for it to count as that road's sidewalk.
SIDEWALK_ROAD_COVERAGE = 0.8


def _is_tagged_sidewalk(tags: dict) -> bool:
    """OSM tags that mark a footway as a street's sidewalk or a road
    crossing, checked before any geometric inference."""
    if tags.get("footway") in _SIDEWALK_FOOTWAY_SUBTAGS:
        return True
    return tags.get("sidewalk") is not None or tags.get("crossing") is not None


def _is_geometric_sidewalk(points: list[tuple[float, float]], road_index: routes_spatial.NearestIndex) -> bool:
    """Fallback for an untagged footway: close to a road-like way along most of
    its length. Resamples first, since OSM nodes on a straight stretch can be
    far enough apart to miss a road running between them."""
    if len(points) < 2:
        return False
    sample_points = _resample([(lat, lng, None) for lat, lng in points])[0]
    close = sum(road_index.nearest(lat, lng, SIDEWALK_ROAD_PROXIMITY_M) is not None for lat, lng in sample_points)
    return close / len(sample_points) >= SIDEWALK_ROAD_COVERAGE


# Classes a drive-to start counts as "on a trail": narrower than
# TRAIL_HIGHWAYS (no cycleway/steps; track, which surface_class reports as
# unpaved_road). A sidewalk footway is excluded either way.
TRAIL_CLASS_HIGHWAYS = frozenset({"path", "footway", "track", "bridleway"})


def is_trail_class(highway: str, surface_class: str) -> bool:
    if surface_class in ("sidewalk", ACCESS_EDGE_SURFACE_CLASS):
        return False
    return highway in TRAIL_CLASS_HIGHWAYS or surface_class == "trail"


_MAXSPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mph|knots|km/h|kmh|kph)?")
_MPH_PER_KPH = 0.621371
_MPH_PER_KNOT = 1.150779


def _parse_maxspeed_mph(raw: str | None) -> float | None:
    if not raw:
        return None
    # A value can list alternatives ("50;30"); like lanes, the first one stands.
    text = raw.split(";")[0].strip().lower()
    if text in ("none", "signals", "walk", "variable"):
        return None
    match = _MAXSPEED_RE.fullmatch(text)
    if match is None:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "mph":
        return value
    if unit == "knots":
        return value * _MPH_PER_KNOT
    return value * _MPH_PER_KPH


def _resolve_maxspeed(highway: str, tags: dict) -> tuple[float, bool]:
    parsed = _parse_maxspeed_mph(tags.get("maxspeed"))
    if parsed is not None:
        return parsed, False
    return DEFAULT_MAXSPEED_MPH_BY_CLASS.get(highway, 25.0), True


def _parse_lanes(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(float(raw.split(";")[0]))
    except ValueError:
        return None


def _has_separation(tags: dict) -> bool:
    for key in _SEPARATION_TAG_KEYS:
        value = tags.get(key)
        if value and value.lower() in _SEPARATION_VALUES:
            return True
    return False


def _structure(tags: dict) -> str | None:
    """'bridge' or 'tunnel' for a way that carries its path over or under
    something else, which is where a section is naturally cut."""
    if tags.get("bridge") not in (None, "", "no"):
        return "bridge"
    if tags.get("tunnel") not in (None, "", "no"):
        return "tunnel"
    return None


def _amenity_kind(tags: dict) -> str | None:
    if tags.get("highway") == "trailhead":
        return "trailhead"
    amenity = tags.get("amenity")
    if amenity in ("parking", "drinking_water", "toilets"):
        return amenity
    return None


_SEPARATION_LIFT = {"uncomfortable": "tolerable", "tolerable": "comfortable", "comfortable": "comfortable"}


def road_comfort_tier(
    highway: str,
    surface_class: str,
    maxspeed_mph: float,
    lanes: int | None,
    has_separation: bool,
    prefs: dict,
) -> str:
    rc = prefs["road_comfort"]
    comfortable_max = rc["comfortable_max_mph"]
    uncomfortable_min = rc["uncomfortable_min_mph"]
    comfortable_classes = set(rc["comfortable_paved_classes"])
    through_classes = set(rc["through_traffic_classes"])

    if maxspeed_mph >= uncomfortable_min or highway in through_classes:
        tier = "uncomfortable"
    elif surface_class == "paved":
        tier = "comfortable" if maxspeed_mph <= comfortable_max and highway in comfortable_classes else "tolerable"
    else:
        tier = "comfortable"

    # A separated path lifts the road one tier; it never makes a fast or
    # through road comfortable on its own.
    if has_separation:
        tier = _SEPARATION_LIFT[tier]

    # More lanes carries more traffic even at a class/speed that would
    # otherwise read as comfortable.
    if lanes is not None and lanes >= 3 and tier == "comfortable":
        tier = "tolerable"
    return tier


# ---------------------------------------------------------------------------
# Point packing
# ---------------------------------------------------------------------------

_POINT_FIELDS = 4  # lat, lng, dist_m, ele_m (NaN sentinel for missing elevation)


def _pack_points(
    latlng: list[tuple[float, float]], dist_m: list[float], ele_m: list[float | None]
) -> bytes:
    flat: list[float] = []
    for (lat, lng), d, e in zip(latlng, dist_m, ele_m):
        flat.extend((lat, lng, d, e if e is not None else math.nan))
    return struct.pack(f"<{len(latlng) * _POINT_FIELDS}d", *flat)


def unpack_edge_points(blob: bytes) -> list[tuple[float, float, float, float | None]]:
    count = len(blob) // (8 * _POINT_FIELDS)
    flat = struct.unpack(f"<{count * _POINT_FIELDS}d", blob)
    out: list[tuple[float, float, float, float | None]] = []
    for i in range(count):
        lat, lng, d, e = flat[i * 4:(i + 1) * 4]
        out.append((lat, lng, d, None if math.isnan(e) else e))
    return out


def index_edge(index: routes_spatial.NearestIndex, key: Hashable, points_blob: bytes) -> None:
    """Add an edge's stored sample points to a NearestIndex under key."""
    index.add(key, ((lat, lng, d) for lat, lng, d, _ele in unpack_edge_points(bytes(points_blob))))


def _resample(raw_points: list[gpx_parse.GpxPoint]) -> tuple[list[tuple[float, float]], list[float], float]:
    """(lat/lng, distance along) of raw_points resampled to gpx_parse's
    spacing, and their total length."""
    dist_m = gpx_parse.cumulative_distances_m(raw_points)
    resampled = gpx_parse.resample_points(raw_points, dist_m)
    return [(p[0], p[1]) for p in resampled], [p[2] for p in resampled], dist_m[-1]


# ---------------------------------------------------------------------------
# Edge splitting
# ---------------------------------------------------------------------------

# A snap point this close to an edge end reuses that end's node.
MIN_SPLIT_CLEARANCE_M = 1.0

# An amenity closer than this to its snap point sits on the network. Farther
# out, a junction goes at the snap point and the amenity keeps its surveyed
# position behind a straight-line access edge, so the walkable gap still
# counts in any path measure.
ACCESS_SPUR_MIN_M = 5.0
ACCESS_EDGE_HIGHWAY = "footway"
# Surface class of an access spur: a connector, not trail mileage.
ACCESS_EDGE_SURFACE_CLASS = "connector"

# Amenity ids are OSM node ids or negated way ids, so both signs are in use.
# Synthetic junction and spur way ids sit this far below that range.
SYNTHETIC_ID_BASE = -(1 << 62)


def synthetic_id_for(amenity_osm_id: int) -> int:
    """The osm_node_id of the junction an amenity's access spur attaches to,
    also used as that spur's way_id."""
    return SYNTHETIC_ID_BASE + amenity_osm_id


class EdgeSplit(TypedDict):
    """The two edge-row-shaped pieces from cutting an edge at offset_m, plus the
    new node's position."""
    before: dict
    after: dict
    node_lat: float
    node_lng: float


# The way-level columns every piece of a cut edge inherits.
_WAY_COLUMNS = (
    "way_id", "way_name", "highway", "surface_class", "oneway", "maxspeed_mph", "maxspeed_filled", "lanes",
    "has_separation", "trail_kind", "structure",
)


def split_edge_row(row: dict, offset_m: float) -> EdgeSplit | None:
    """Cut an edge-row-shaped mapping (anything answering row["field"] for the
    net_edges columns) at offset_m along its forward direction. None when
    either piece would be under
    MIN_SPLIT_CLEARANCE_M. Used at build time for amenities and, unpersisted,
    for a search's own start point."""
    length_m = row["length_m"]
    if offset_m < MIN_SPLIT_CLEARANCE_M or offset_m > length_m - MIN_SPLIT_CLEARANCE_M:
        return None

    points = unpack_edge_points(bytes(row["points_blob"]))
    dist_m = [p[2] for p in points]
    lat_vals = cast("list[float | None]", [p[0] for p in points])
    lng_vals = cast("list[float | None]", [p[1] for p in points])
    ele_vals: list[float | None] = [p[3] for p in points]

    def piece(from_m: float, to_m: float) -> tuple[dict, list[float | None], list[float | None]]:
        dist, lat = routes_spatial.slice_by_distance(dist_m, lat_vals, from_m, to_m)
        _, lng = routes_spatial.slice_by_distance(dist_m, lng_vals, from_m, to_m)
        _, ele = routes_spatial.slice_by_distance(dist_m, ele_vals, from_m, to_m)
        has_elevation = bool(row["has_elevation"]) and all(e is not None for e in ele)
        latlng = list(zip(cast("list[float]", lat), cast("list[float]", lng)))
        edge = {c: row[c] for c in _WAY_COLUMNS} | {
            "length_m": dist[-1] if dist else 0.0,
            "has_elevation": int(has_elevation),
            "points_blob": _pack_points(latlng, dist, ele),
        }
        return edge, lat, lng

    before, lat_before, lng_before = piece(0.0, offset_m)
    after, _, _ = piece(offset_m, length_m)
    return {
        "before": before,
        "after": after,
        "node_lat": cast(float, lat_before[-1]),
        "node_lng": cast(float, lng_before[-1]),
    }


@dataclass(frozen=True)
class EdgeCut:
    """Where splice_at snapped a point. split is None when a piece would be too
    short (nothing changed); otherwise the pieces are in rows under before_key
    and after_key."""
    edge_key: int
    row: dict
    offset_m: float
    split: EdgeSplit | None
    before_key: int = 0
    after_key: int = 0


def splice_at(
    rows: MutableMapping[int, dict],
    index: routes_spatial.NearestIndex,
    lat: float,
    lng: float,
    radius_m: float,
    ids: Iterable[int],
    *,
    clamp: bool = False,
) -> EdgeCut | None:
    """Snap (lat, lng) onto the nearest edge of index within radius_m and cut it
    there. The pieces are added to rows under fresh ids from ids and replace
    the original in index; the original row stays in rows for the caller to
    drop. clamp moves the cut off an edge's ends. None when nothing is within
    radius_m."""
    found = index.nearest(lat, lng, radius_m)
    if found is None:
        return None
    edge_key, offset_m = cast(int, found[0]), found[1]
    row = rows[edge_key]
    if clamp:
        offset_m = min(max(offset_m, MIN_SPLIT_CLEARANCE_M), row["length_m"] - MIN_SPLIT_CLEARANCE_M)
    split = split_edge_row(row, offset_m)
    if split is None:
        return EdgeCut(edge_key, row, offset_m, None)
    id_iter = iter(ids)
    before_key, after_key = next(id_iter), next(id_iter)
    rows[before_key], rows[after_key] = split["before"], split["after"]
    index.remove(edge_key)
    index_edge(index, before_key, split["before"]["points_blob"])
    index_edge(index, after_key, split["after"]["points_blob"])
    return EdgeCut(edge_key, row, offset_m, split, before_key, after_key)


_DEFAULT_EDGE_ATTRS: dict[str, object] = {
    "way_name": None,
    "oneway": "no",
    "maxspeed_mph": None,
    "maxspeed_filled": 0,
    "lanes": None,
    "has_separation": 0,
    "trail_kind": None,
    "structure": None,
}


def _edge_row(
    sampler: ElevationSampler,
    from_osm: int,
    to_osm: int,
    raw_points: list[gpx_parse.GpxPoint],
    attrs: Mapping[str, object],
) -> dict | None:
    """An edge-row-shaped dict between two OSM node ids, resampled and draped;
    None for a zero-length piece. attrs holds the way-level columns (way_id,
    highway and surface_class at least) over _DEFAULT_EDGE_ATTRS."""
    latlng, dist, length_m = _resample(raw_points)
    if length_m <= 0:
        return None
    elevations = sampler.sample(latlng)
    has_elevation = len(elevations) == len(latlng) and all(e is not None for e in elevations)
    ele_out: list[float | None] = cast(list[float | None], elevations) if has_elevation else [None] * len(latlng)
    return {
        **_DEFAULT_EDGE_ATTRS,
        **attrs,
        "from_osm": from_osm,
        "to_osm": to_osm,
        "length_m": length_m,
        "has_elevation": int(has_elevation),
        "points_blob": _pack_points(latlng, dist, ele_out),
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_network(
    conn: sqlite3.Connection,
    area_id: int,
    payloads: Iterable[dict],
    sampler: ElevationSampler,
) -> NetworkStats:
    """Rebuild an area's graph and its sections wholesale from raw tile
    payloads in one transaction; a failure rolls back to the previous ones."""
    # sections imports this module, so it can only be imported here.
    from . import sections

    try:
        scenery = sections.Scenery()
        stats = _build_network(conn, area_id, payloads, sampler, scenery)
        # After _build_network returns, so its working set is already freed.
        sections.rebuild_sections(conn, area_id, scenery)
        conn.commit()
        return stats
    except BaseException:
        conn.rollback()
        raise


def _build_network(
    conn: sqlite3.Connection,
    area_id: int,
    payloads: Iterable[dict],
    sampler: ElevationSampler,
    scenery: "sections.Scenery",
) -> NetworkStats:
    """Write the graph, uncommitted, and collect the payloads' scenery into
    `scenery`. Tiles overlap, so a way can appear in several payloads; ways
    are deduped by OSM id first."""
    ways: dict[int, dict] = {}
    node_coords: dict[int, tuple[float, float]] = {}
    amenity_nodes: dict[int, tuple[str, float, float, str | None]] = {}

    for payload in payloads:
        for el in payload.get("elements", []):
            scenery.add(el)
            el_type = el.get("type")
            if el_type == "way":
                way_id = el.get("id")
                if way_id is not None and way_id not in ways:
                    ways[way_id] = el
            elif el_type == "node":
                node_id = el.get("id")
                if node_id is None:
                    continue
                lat, lon = el.get("lat"), el.get("lon")
                if lat is not None and lon is not None:
                    node_coords.setdefault(node_id, (lat, lon))
                    tags = el.get("tags") or {}
                    kind = _amenity_kind(tags)
                    if kind == "parking" and tags.get("access") in ("private", "no"):
                        kind = None
                    if kind is not None:
                        amenity_nodes.setdefault(node_id, (kind, lat, lon, tags.get("name")))

    # "out geom" payloads carry node ids and geometry as parallel arrays on the
    # way; harvest coordinates from there too.
    for way in ways.values():
        geom = way.get("geometry")
        node_ids = way.get("nodes") or []
        if geom and len(geom) == len(node_ids):
            for nid, pt in zip(node_ids, geom):
                if pt is not None:
                    node_coords.setdefault(nid, (pt["lat"], pt["lon"]))

    routable_ways: list[tuple[int, dict, list[tuple[float, float]]]] = []
    excluded_count = 0

    for way_id, way in ways.items():
        tags = way.get("tags") or {}
        highway = tags.get("highway")
        if not highway:
            continue
        points = _way_points(way, node_coords)
        if points is None:
            continue
        if highway not in TRAIL_HIGHWAYS and highway not in ROAD_LIKE_HIGHWAYS:
            continue
        if not _way_allowed(highway, tags):
            excluded_count += 1
            continue
        node_ids = way.get("nodes") or []
        if len(node_ids) != len(points):
            excluded_count += 1
            continue
        routable_ways.append((way_id, way, points))

    node_way_count: dict[int, int] = {}
    for _way_id, way, _points in routable_ways:
        for nid in set(way["nodes"]):
            node_way_count[nid] = node_way_count.get(nid, 0) + 1
    graph_node_ids: set[int] = {nid for nid, c in node_way_count.items() if c >= 2}
    for _way_id, way, _points in routable_ways:
        node_ids = way["nodes"]
        graph_node_ids.add(node_ids[0])
        graph_node_ids.add(node_ids[-1])

    # Assembled in memory by OSM node id, written in one short transaction.
    nodes: dict[int, dict] = {}

    def ensure_node(osm_node_id: int, lat: float, lng: float) -> None:
        if osm_node_id not in nodes:
            kind = "junction" if node_way_count.get(osm_node_id, 0) >= 2 else "endpoint"
            nodes[osm_node_id] = {"kind": kind, "name": None, "lat": lat, "lng": lng}

    # Resampled: street nodes can be 100m+ apart, which would miss a sidewalk
    # running between two of them.
    road_points: list[tuple[float, float]] = []
    for way in ways.values():
        if (way.get("tags") or {}).get("highway") not in SIDEWALK_ROAD_HIGHWAYS:
            continue
        way_points = _way_points(way, node_coords)
        if way_points is None:
            continue
        road_points.extend(_resample([(lat, lng, None) for lat, lng in way_points])[0])
    road_index = routes_spatial.point_index(road_points, SIDEWALK_ROAD_PROXIMITY_M)

    edges: dict[int, dict] = {}
    edge_ids = itertools.count()
    edge_index = routes_spatial.NearestIndex(cell_size_m=50.0)  # keyed like edges

    for way_id, way, points in routable_ways:
        tags = way.get("tags") or {}
        highway = tags["highway"]
        node_ids = way["nodes"]
        split_at = sorted({0, len(node_ids) - 1} | {i for i, nid in enumerate(node_ids) if nid in graph_node_ids})

        is_road_like = highway in ROAD_LIKE_HIGHWAYS
        surface_class = _surface_class(highway, tags)
        # Checked whatever the surface class says: sidewalks are often paved.
        if highway == "footway" and (_is_tagged_sidewalk(tags) or _is_geometric_sidewalk(points, road_index)):
            surface_class = "sidewalk"
        way_attrs: dict[str, object] = {
            "way_id": way_id,
            "highway": highway,
            "surface_class": surface_class,
            "trail_kind": _trail_kind(highway, surface_class, tags),
            "structure": _structure(tags),
            "way_name": tags.get("name"),
            "oneway": _oneway_value(highway, tags),
        }
        if is_road_like:
            maxspeed_mph, maxspeed_filled = _resolve_maxspeed(highway, tags)
            way_attrs.update(
                maxspeed_mph=maxspeed_mph,
                maxspeed_filled=int(maxspeed_filled),
                lanes=_parse_lanes(tags.get("lanes")),
                has_separation=int(_has_separation(tags)),
            )

        for seg_start, seg_end in zip(split_at, split_at[1:]):
            seg_points = points[seg_start:seg_end + 1]
            if len(seg_points) < 2:
                continue
            from_osm, to_osm = node_ids[seg_start], node_ids[seg_end]
            from_coord = node_coords.get(from_osm, seg_points[0])
            to_coord = node_coords.get(to_osm, seg_points[-1])
            ensure_node(from_osm, from_coord[0], from_coord[1])
            ensure_node(to_osm, to_coord[0], to_coord[1])

            row = _edge_row(sampler, from_osm, to_osm, [(lat, lng, None) for lat, lng in seg_points], way_attrs)
            if row is None:
                continue
            edge_key = next(edge_ids)
            index_edge(edge_index, edge_key, row["points_blob"])
            edges[edge_key] = row

    # An amenity whose OSM node is already a routing node is that node: promote
    # its kind so it is findable as a drive-to start, and cut no edge for it
    # (that would split off a self-loop).
    resolved: set[int] = set()
    for osm_id, (kind, _lat, _lng, name) in amenity_nodes.items():
        routing_node = nodes.get(osm_id)
        if routing_node is None:
            continue
        if kind in ("trailhead", "parking"):
            routing_node["kind"] = kind
            if routing_node["name"] is None:
                routing_node["name"] = name
        resolved.add(osm_id)

    # Each cut replaces its edge in the index, so a later amenity on the same
    # edge snaps onto the right piece. Amenities that resolve onto an existing
    # node or have no edge to join are added below at their surveyed position.
    for osm_id, (kind, amenity_lat, amenity_lng, name) in amenity_nodes.items():
        if osm_id in resolved:
            continue
        cut = splice_at(edges, edge_index, amenity_lat, amenity_lng, AMENITY_SNAP_RADIUS_M, edge_ids, clamp=True)
        if cut is None:
            continue
        resolved.add(osm_id)
        if cut.split is None:
            # Too short to cut: the amenity uses the closer existing node.
            continue
        snap_ll = (cut.split["node_lat"], cut.split["node_lng"])
        if routes_spatial.equirect_distance_m(amenity_lat, amenity_lng, *snap_ll) < ACCESS_SPUR_MIN_M:
            on_edge_osm = osm_id
            nodes[osm_id] = {"kind": kind, "name": name, "lat": snap_ll[0], "lng": snap_ll[1]}
        else:
            # The junction gets a synthetic osm_node_id; the amenity keeps its
            # surveyed position. The straight-line spur is draped like any
            # edge so a path through it doesn't read as partial elevation.
            on_edge_osm = synthetic_id_for(osm_id)
            nodes[on_edge_osm] = {"kind": "junction", "name": None, "lat": snap_ll[0], "lng": snap_ll[1]}
            nodes[osm_id] = {"kind": kind, "name": name, "lat": amenity_lat, "lng": amenity_lng}
            spur = _edge_row(
                sampler, on_edge_osm, osm_id, [(*snap_ll, None), (amenity_lat, amenity_lng, None)],
                {"way_id": on_edge_osm, "highway": ACCESS_EDGE_HIGHWAY, "surface_class": ACCESS_EDGE_SURFACE_CLASS},
            )
            if spur is not None:
                edges[next(edge_ids)] = spur
        edges[cut.before_key].update(from_osm=cut.row["from_osm"], to_osm=on_edge_osm)
        edges[cut.after_key].update(from_osm=on_edge_osm, to_osm=cut.row["to_osm"])
        del edges[cut.edge_key]

    for osm_id, (kind, lat, lng, name) in amenity_nodes.items():
        if osm_id not in resolved:
            nodes[osm_id] = {"kind": kind, "name": name, "lat": lat, "lng": lng}

    final_edges = list(edges.values())
    _replace_area_graph(conn, area_id, nodes, final_edges, excluded_count)

    return {
        "area_id": area_id,
        "node_count": len(nodes),
        "edge_count": len(final_edges),
        "amenity_count": len(amenity_nodes),
        "excluded_way_count": excluded_count,
    }


_EDGE_COLUMNS = (*_WAY_COLUMNS, "length_m", "has_elevation", "points_blob")


def _replace_area_graph(
    conn: sqlite3.Connection, area_id: int, nodes: dict[int, dict], edges: list[dict], excluded_count: int
) -> None:
    """Swap an area's nodes and edges for the given ones; the caller commits."""
    conn.execute("DELETE FROM net_edges WHERE area_id = ?", [area_id])
    conn.execute("DELETE FROM net_nodes WHERE area_id = ?", [area_id])
    first_id = conn.execute("SELECT COALESCE(MAX(node_id), 0) + 1 FROM net_nodes").fetchone()[0]
    node_ids = {osm_id: first_id + i for i, osm_id in enumerate(nodes)}
    conn.executemany(
        "INSERT INTO net_nodes (node_id, area_id, osm_node_id, kind, name, lat, lng) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(node_ids[osm_id], area_id, osm_id, n["kind"], n["name"], n["lat"], n["lng"]) for osm_id, n in nodes.items()],
    )
    columns = ", ".join(("area_id", "from_node_id", "to_node_id", *_EDGE_COLUMNS))
    placeholders = ", ".join("?" for _ in range(len(_EDGE_COLUMNS) + 3))
    conn.executemany(
        f"INSERT INTO net_edges ({columns}) VALUES ({placeholders})",
        [
            (area_id, node_ids[e["from_osm"]], node_ids[e["to_osm"]], *(e[c] for c in _EDGE_COLUMNS))
            for e in edges
        ],
    )
    conn.execute(
        """
        INSERT INTO net_graphs (area_id, derive_version, built_at, node_count, edge_count, excluded_way_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(area_id) DO UPDATE SET
            derive_version = excluded.derive_version, built_at = excluded.built_at,
            node_count = excluded.node_count, edge_count = excluded.edge_count,
            excluded_way_count = excluded.excluded_way_count
        """,
        [area_id, NETWORK_DERIVE_VERSION, datetime.now(timezone.utc).isoformat(), len(nodes), len(edges), excluded_count],
    )


# ---------------------------------------------------------------------------
# Path scoring
# ---------------------------------------------------------------------------


def _road_exposure(
    entries: list[tuple[str | None, float, int | None]], max_uncomfortable_m: float
) -> RoadExposure:
    """Contiguous-uncomfortable-road tracking: a run ends only at a
    non-uncomfortable edge, so two stretches split by anything else are two
    runs, not one summed cost."""
    totals: dict[str, float] = {"comfortable": 0.0, "tolerable": 0.0, "uncomfortable": 0.0}
    violations: list[RoadExposureViolation] = []
    avoided_ways: set[int] = set()
    run_len = 0.0
    run_start = 0.0
    max_run = 0.0
    cum = 0.0

    for tier, length_m, avoided_way in entries:
        if tier is not None:
            totals[tier] += length_m
        if avoided_way is not None:
            avoided_ways.add(avoided_way)
        if tier == "uncomfortable":
            if run_len == 0.0:
                run_start = cum
            run_len += length_m
        else:
            if run_len > max_uncomfortable_m:
                violations.append({"start_m": run_start, "end_m": cum, "length_m": run_len})
            max_run = max(max_run, run_len)
            run_len = 0.0
        cum += length_m

    if run_len > max_uncomfortable_m:
        violations.append({"start_m": run_start, "end_m": cum, "length_m": run_len})
    max_run = max(max_run, run_len)

    return {
        "max_continuous_uncomfortable_m": max_run,
        "violations": violations,
        "totals_m": totals,
        "avoided_ways": sorted(avoided_ways),
    }


def score_path(
    conn: sqlite3.Connection,
    area_id: int,
    edge_sequence: Sequence[EdgeSequenceEntry],
    prefs: dict,
    runnability: RunnabilityFn,
    extra_edges: Mapping[int, dict] | None = None,
) -> PathScore:
    """extra_edges supplies edge-row-shaped mappings consulted before the
    database: for ids absent from net_edges (a search's spliced start point)
    and to skip the query when the caller already holds the edges."""
    if not edge_sequence:
        raise ValueError("edge_sequence must have at least one edge")

    edge_ids = [eid for eid, _ in edge_sequence]
    rows: dict[int, dict] = {}
    if extra_edges:
        for eid in edge_ids:
            row = extra_edges.get(eid)
            if row is not None:
                rows[eid] = row
    missing_ids = [eid for eid in edge_ids if eid not in rows]
    if missing_ids:
        placeholders = ",".join("?" for _ in missing_ids)
        rows.update({
            r["edge_id"]: r
            for r in conn.execute(f"SELECT * FROM net_edges WHERE area_id = ? AND edge_id IN ({placeholders})", [area_id, *missing_ids]).fetchall()
        })
    way_ids = sorted({rows[eid]["way_id"] for eid in edge_ids if eid in rows})
    overrides = get_way_overrides(conn, way_ids)

    distance_m = trail_m = unpaved_m = paved_m = connector_m = singletrack_m = track_m = 0.0
    elevation_m = 0.0
    # Consecutive edges with elevation form one profile, as one stored route
    # would; an edge without it ends the stretch.
    stretches: list[list[gpx_parse.GpxPoint]] = []
    stretch: list[gpx_parse.GpxPoint] | None = None
    exposure_entries: list[tuple[str | None, float, int | None]] = []

    for edge_id, forward in edge_sequence:
        row = rows.get(edge_id)
        if row is None:
            raise ValueError(f"no such edge in area {area_id}: {edge_id}")
        length_m = row["length_m"]
        distance_m += length_m
        surface_class = row["surface_class"]
        trail_kind = row["trail_kind"]
        if trail_kind == TRAIL_KIND_SINGLETRACK:
            singletrack_m += length_m
        elif trail_kind == TRAIL_KIND_TRACK:
            track_m += length_m
        if surface_class == "trail" or trail_kind is not None:
            trail_m += length_m
        elif surface_class == "unpaved_road":
            unpaved_m += length_m
        elif surface_class == ACCESS_EDGE_SURFACE_CLASS:
            connector_m += length_m
        else:
            paved_m += length_m

        if row["has_elevation"]:
            elevation_m += length_m
            if stretch is None:
                stretch = []
                stretches.append(stretch)
            points = unpack_edge_points(bytes(row["points_blob"]))
            for lat, lng, _d, ele in points if forward else reversed(points):
                if not (stretch and stretch[-1][0] == lat and stretch[-1][1] == lng):
                    stretch.append((lat, lng, ele))
        else:
            stretch = None

        way_id = row["way_id"]
        override = overrides.get(way_id)
        tier: str | None = None
        avoided_way_id: int | None = way_id if override == "avoid" else None
        if row["maxspeed_mph"] is not None:
            tier = road_comfort_tier(
                row["highway"], surface_class, row["maxspeed_mph"], row["lanes"], bool(row["has_separation"]), prefs
            )
            if override == "comfortable":
                tier = "comfortable"
            elif override == "avoid":
                tier = "uncomfortable"
        exposure_entries.append((tier, length_m, avoided_way_id))

    gain_total = loss_total = 0.0
    grade_profile: list[float] = []
    for points in stretches:
        dist = gpx_parse.cumulative_distances_m(points)
        smoothed, _half_window = gpx_parse.roughness_smoothed(dist, cast("list[float]", [p[2] for p in points]))
        gain_m, loss_m = gpx_parse.gain_loss_m(smoothed)
        gain_total += gain_m
        loss_total += loss_m
        grade_profile.extend(
            (e1 - e0) / (d1 - d0) * 100.0 for e0, e1, d0, d1 in zip(smoothed, smoothed[1:], dist, dist[1:]) if d1 > d0
        )

    return {
        "distance_m": distance_m,
        "trail_distance_m": trail_m,
        "singletrack_distance_m": singletrack_m,
        "track_distance_m": track_m,
        "unpaved_road_distance_m": unpaved_m,
        "paved_distance_m": paved_m,
        "connector_distance_m": connector_m,
        "gain_m": gain_total if stretches else None,
        "loss_m": loss_total if stretches else None,
        "has_elevation": bool(stretches) and elevation_m >= distance_m,
        "elevation_coverage_pct": 100.0 * min(elevation_m / distance_m, 1.0) if distance_m > 0 else 0.0,
        "runnability": runnability(grade_profile),
        "road_exposure": _road_exposure(exposure_entries, prefs["max_continuous_uncomfortable_m"]),
    }
