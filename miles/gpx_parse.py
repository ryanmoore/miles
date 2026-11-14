"""GPX parsing and per-route derived math: distance, elevation smoothing,
resampling, climb segmentation, and grade-band histograms.

Untrusted uploads, so parsing goes through defusedxml rather than the stdlib
XML modules. GPX 1.0 and 1.1 declare different namespaces for the same
elements, so every lookup matches by local tag name (ignoring the namespace
URI) rather than a hardcoded namespace map.

Elevation gain/loss is a centered moving average followed by a hysteresis
threshold: raw point-to-point deltas count GPS/DEM jitter as climbing.

KML files (Google Earth / My Maps / CalTopo exports) parse into the same
GpxDocument: each Placemark's LineString / gx:Track becomes a track and each
Point placemark a waypoint. The raw KML bytes are what gets stored; the GPX
download converts on the fly (document_to_gpx).
"""

import math
from typing import Literal
from typing_extensions import TypedDict

import defusedxml.ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import Element, ParseError

from xml.sax.saxutils import escape as xml_escape

from .routes_spatial import equirect_distance_m

# Elevation deltas smaller than this, measured from the last point that
# itself cleared the threshold, are noise: without a floor, jitter on flat
# ground sums to a large fake gain.
ELEVATION_NOISE_M = 1.0

# Longest half-width, in meters of along-route distance, of the moving
# average applied before the threshold above. A distance rather than a point
# count, because source point spacing varies from a few meters to twenty-plus.
ELEVATION_SMOOTHING_HALF_WINDOW_M = 50.0

# Noise floor used only when pairing turning points into climb/descent
# segments (segment_climbs). Kept separate from ELEVATION_NOISE_M so a
# tighter gain/loss noise floor doesn't fragment one sustained climb into
# many small segments.
CLIMB_SEGMENTATION_NOISE_M = 3.0

# Target spacing for the resampled route_points table.
RESAMPLE_SPACING_M = 20.0

# Roughness (profile_roughness_m) at or below ROUGHNESS_SMOOTH_M gets no
# smoothing, at or above ROUGHNESS_NOISY_M the full window, and the window
# scales linearly between (adr/0002-routes-raw-gpx-ground-truth.md). The
# thresholds are calibrated to profiles on the RESAMPLE_SPACING_M grid.
ROUGHNESS_SMOOTH_M = 0.3
ROUGHNESS_NOISY_M = 1.5

# Minimum |elevation change| for a turning-point pair to count as a
# reported climb/descent segment -- well above ELEVATION_NOISE_M, which
# only filters point-to-point jitter. A sustained climb is a much bigger
# feature than a single noisy step.
CLIMB_MIN_GAIN_M = 30.0

# (lo, hi, label) grade bands, grade expressed as a fraction (0.10 = 10%).
# Bounds are lo <= grade < hi except the last band, which is closed at hi.
GRADE_BANDS: list[tuple[float, float, str]] = [
    (float("-inf"), -0.10, "<-10"),
    (-0.10, -0.05, "-10..-5"),
    (-0.05, -0.02, "-5..-2"),
    (-0.02, 0.02, "flat"),
    (0.02, 0.05, "2..5"),
    (0.05, 0.10, "5..10"),
    (0.10, float("inf"), ">10"),
]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# The GPX 1.1 default namespace -- required on a root element for the file
# to validate against the schema most consumers (Strava included) expect.
GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"

# lat, lng, elevation in meters (None when the point carries no <ele>)
GpxPoint = tuple[float, float, float | None]


class GpxTrack(TypedDict):
    kind: Literal["trk", "rte"]
    track_index: int
    gpx_name: str | None
    points: list[GpxPoint]


class GpxWaypoint(TypedDict):
    name: str | None
    desc: str | None
    lat: float
    lng: float


class GpxDocument(TypedDict):
    creator: str | None
    tracks: list[GpxTrack]
    waypoints: list[GpxWaypoint]


class ClimbSegment(TypedDict):
    kind: Literal["climb", "descent"]
    start_m: float
    end_m: float
    gain_m: float
    avg_grade: float


class GpxParseError(ValueError):
    """Raised when a file isn't parseable XML or has no usable tracks/routes."""


def _local_tag(elem: Element) -> str:
    tag = elem.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(elem: Element, tag: str) -> str | None:
    for child in elem:
        if _local_tag(child) == tag:
            text = (child.text or "").strip()
            return text or None
    return None


def _parse_ele(point_elem: Element) -> float | None:
    for child in point_elem:
        if _local_tag(child) == "ele" and child.text:
            try:
                return float(child.text)
            except ValueError:
                return None
    return None


def _parse_point(elem: Element) -> GpxPoint | None:
    lat = elem.get("lat")
    lon = elem.get("lon")
    if lat is None or lon is None:
        return None
    return (float(lat), float(lon), _parse_ele(elem))


def _parse_root(data: bytes) -> Element:
    try:
        root = DefusedET.fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise GpxParseError(f"Not valid GPX/KML/XML: {exc}") from exc
    if not isinstance(root, Element):
        raise GpxParseError("Not valid GPX/KML/XML: unexpected document root")
    return root


def is_kml(data: bytes) -> bool:
    """True when the bytes are a KML document (root element <kml>)."""
    try:
        return _local_tag(_parse_root(data)) == "kml"
    except GpxParseError:
        return False


def parse_gpx_bytes(data: bytes) -> GpxDocument:
    """Parse raw GPX (or KML, see parse_kml_root) bytes into every trk/rte (as
    one entry per track, in document order) and every file-level wpt. Multiple
    trkseg within one trk are flattened into a single point list -- segment
    breaks aren't tracked. Tracks/routes with fewer than two points are
    dropped as unusable.
    """
    root = _parse_root(data)
    if _local_tag(root) == "kml":
        return parse_kml_root(root)

    tracks: list[GpxTrack] = []
    for child in root:
        tag = _local_tag(child)
        if tag == "trk":
            points: list[GpxPoint] = []
            for seg in child:
                if _local_tag(seg) != "trkseg":
                    continue
                for trkpt in seg:
                    if _local_tag(trkpt) != "trkpt":
                        continue
                    p = _parse_point(trkpt)
                    if p is not None:
                        points.append(p)
            if len(points) >= 2:
                tracks.append(
                    {
                        "kind": "trk",
                        "track_index": len(tracks),
                        "gpx_name": _child_text(child, "name"),
                        "points": points,
                    }
                )
        elif tag == "rte":
            points = []
            for rtept in child:
                if _local_tag(rtept) != "rtept":
                    continue
                p = _parse_point(rtept)
                if p is not None:
                    points.append(p)
            if len(points) >= 2:
                tracks.append(
                    {
                        "kind": "rte",
                        "track_index": len(tracks),
                        "gpx_name": _child_text(child, "name"),
                        "points": points,
                    }
                )
    if not tracks:
        raise GpxParseError("No trk or rte with at least two points")

    waypoints: list[GpxWaypoint] = []
    for child in root:
        if _local_tag(child) != "wpt":
            continue
        p = _parse_point(child)
        if p is None:
            continue
        waypoints.append(
            {
                "name": _child_text(child, "name"),
                "desc": _child_text(child, "desc"),
                "lat": p[0],
                "lng": p[1],
            }
        )

    return {"creator": root.get("creator"), "tracks": tracks, "waypoints": waypoints}


def _kml_coordinates(text: str | None) -> list[GpxPoint]:
    """KML <coordinates>: whitespace-separated "lon,lat[,alt]" tuples."""
    points: list[GpxPoint] = []
    for tup in (text or "").split():
        parts = tup.split(",")
        if len(parts) < 2:
            continue
        try:
            lng, lat = float(parts[0]), float(parts[1])
            alt = float(parts[2]) if len(parts) > 2 and parts[2] else None
        except ValueError:
            continue
        points.append((lat, lng, alt))
    return points


def _kml_gx_coords(track: Element) -> list[GpxPoint]:
    """gx:Track: one <gx:coord> per point, "lon lat [alt]" space-separated."""
    points: list[GpxPoint] = []
    for child in track:
        if _local_tag(child) != "coord" or not child.text:
            continue
        parts = child.text.split()
        if len(parts) < 2:
            continue
        try:
            lng, lat = float(parts[0]), float(parts[1])
            alt = float(parts[2]) if len(parts) > 2 else None
        except ValueError:
            continue
        points.append((lat, lng, alt))
    return points


def _kml_line_points(geometry: Element) -> list[GpxPoint]:
    """Every point of the line geometries under a Placemark, in document
    order: LineString, gx:Track, and those nested in MultiGeometry /
    gx:MultiTrack flattened into one list (like trksegs within a trk).
    Polygons and Points are not lines and are skipped."""
    points: list[GpxPoint] = []
    for elem in geometry.iter():
        tag = _local_tag(elem)
        if tag == "LineString":
            points.extend(_kml_coordinates(_child_text(elem, "coordinates")))
        elif tag == "Track":
            points.extend(_kml_gx_coords(elem))
    return points


def _kml_point(placemark: Element) -> GpxPoint | None:
    for elem in placemark.iter():
        if _local_tag(elem) == "Point":
            coords = _kml_coordinates(_child_text(elem, "coordinates"))
            return coords[0] if coords else None
    return None


# Share of a KML path's points at altitude exactly 0 above which the whole
# path's file elevation is dropped. A drawn path (Google Earth, My Maps) is
# clamped to the ground and writes 0 on every point it adds, and a path
# stitched from a recorded stretch plus a drawn one is real elevation that
# falls to 0 partway along. Real ground at exactly 0.0 m on a tenth of a
# route is rare enough to ignore.
KML_ZERO_ALTITUDE_MAX_SHARE = 0.10


def _drop_zero_altitudes(points: list[GpxPoint]) -> list[GpxPoint]:
    """A KML path's altitudes are no elevation at all (rather than a route
    at sea level) when more than KML_ZERO_ALTITUDE_MAX_SHARE of its points
    are 0 or missing; the route is then draped like any file without
    elevation. Keeping the real stretch and dropping only the zeros would
    leave a profile that stops partway."""
    zero = sum(1 for p in points if p[2] is None or p[2] == 0.0)
    if zero > KML_ZERO_ALTITUDE_MAX_SHARE * len(points):
        return [(lat, lng, None) for lat, lng, _ in points]
    return points


def parse_kml_root(root: Element) -> GpxDocument:
    """Every Placemark (at any Document/Folder depth, in document order) with
    a line geometry becomes a "trk" track named by the Placemark's <name>;
    every Placemark whose geometry is a lone Point becomes a waypoint."""
    tracks: list[GpxTrack] = []
    waypoints: list[GpxWaypoint] = []
    for placemark in root.iter():
        if _local_tag(placemark) != "Placemark":
            continue
        points = _kml_line_points(placemark)
        if points:
            if len(points) >= 2:
                tracks.append(
                    {
                        "kind": "trk",
                        "track_index": len(tracks),
                        "gpx_name": _child_text(placemark, "name"),
                        "points": _drop_zero_altitudes(points),
                    }
                )
            continue
        p = _kml_point(placemark)
        if p is not None:
            waypoints.append(
                {
                    "name": _child_text(placemark, "name"),
                    "desc": _child_text(placemark, "description"),
                    "lat": p[0],
                    "lng": p[1],
                }
            )
    if not tracks:
        raise GpxParseError("No KML LineString or gx:Track with at least two points")
    return {"creator": None, "tracks": tracks, "waypoints": waypoints}


def document_to_gpx(doc: GpxDocument, creator: str = "miles") -> bytes:
    """A GPX 1.1 file carrying every track (as trk or rte, per its kind) and
    waypoint of a parsed document -- the download form of a stored KML."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gpx version="1.1" creator="{xml_escape(creator, {chr(34): "&quot;"})}" xmlns="{GPX_NAMESPACE}">',
    ]

    def pt(tag: str, lat: float, lng: float, ele: float | None, inner: str = "") -> str:
        ele_xml = f"<ele>{ele:.2f}</ele>" if ele is not None else ""
        return f'<{tag} lat="{lat:.7f}" lon="{lng:.7f}">{ele_xml}{inner}</{tag}>'

    for w in doc["waypoints"]:
        inner = ""
        if w["name"]:
            inner += f"<name>{xml_escape(w['name'])}</name>"
        if w["desc"]:
            inner += f"<desc>{xml_escape(w['desc'])}</desc>"
        lines.append("  " + pt("wpt", w["lat"], w["lng"], None, inner))
    for t in doc["tracks"]:
        name_xml = f"<name>{xml_escape(t['gpx_name'])}</name>" if t["gpx_name"] else ""
        if t["kind"] == "rte":
            lines.append(f"  <rte>{name_xml}")
            lines += ["    " + pt("rtept", lat, lng, ele) for lat, lng, ele in t["points"]]
            lines.append("  </rte>")
        else:
            lines.append(f"  <trk>{name_xml}<trkseg>")
            lines += ["    " + pt("trkpt", lat, lng, ele) for lat, lng, ele in t["points"]]
            lines.append("  </trkseg></trk>")
    lines.append("</gpx>")
    return "\n".join(lines).encode("utf-8")


def cumulative_distances_m(points: list[GpxPoint]) -> list[float]:
    """Cumulative equirectangular distance along points, dist[0] == 0.0."""
    dist = [0.0]
    for i in range(1, len(points)):
        d = equirect_distance_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        dist.append(dist[-1] + d)
    return dist


def has_full_elevation(points: list[GpxPoint]) -> bool:
    """True only when every point carries <ele>. A track with some points
    missing elevation is treated the same as one with none -- a partial
    profile can't support gain/grade stats any more honestly than no
    profile at all, and mixing real and absent values would silently bias
    the smoothing window."""
    return all(p[2] is not None for p in points)


def smoothed_elevations(
    elevations: list[float], dist_m: list[float], half_window_m: float = ELEVATION_SMOOTHING_HALF_WINDOW_M
) -> list[float]:
    """Centered moving average over a +/-half_window_m window of cumulative
    distance; edge points average over whatever falls inside the series.
    `dist_m` is the cumulative distance of each point (same length,
    non-decreasing). A half_window_m of 0 averages each point with only
    itself and any co-located duplicates.

    Applied before gain_loss_m's threshold, not after: per-point jitter is
    on the scale of ELEVATION_NOISE_M, so the threshold alone would count a
    climb that arrives as a staircase of small steps twice. Both window
    edges only move forward, so the pass is O(n).
    """
    n = len(elevations)
    half = half_window_m
    out = [0.0] * n
    lo = 0
    hi = -1
    total = 0.0
    count = 0
    for i in range(n):
        while hi + 1 < n and dist_m[hi + 1] - dist_m[i] <= half:
            hi += 1
            total += elevations[hi]
            count += 1
        while dist_m[i] - dist_m[lo] > half:
            total -= elevations[lo]
            count -= 1
            lo += 1
        out[i] = total / count
    return out


def gain_loss_m(smoothed: list[float]) -> tuple[float, float]:
    """Cumulative gain/loss in meters over a smoothed elevation series. A
    delta only counts against the last point that itself cleared
    ELEVATION_NOISE_M, not the immediately preceding point -- otherwise a
    staircase of sub-threshold jitter would never accumulate into a real
    climb, and small noise on flat ground would never get filtered."""
    if len(smoothed) < 2:
        return 0.0, 0.0
    gain = 0.0
    loss = 0.0
    last = smoothed[0]
    for ele in smoothed[1:]:
        delta = ele - last
        if abs(delta) < ELEVATION_NOISE_M:
            continue
        if delta > 0:
            gain += delta
        else:
            loss += -delta
        last = ele
    return gain, loss


def _even_targets(total: float, spacing_m: float) -> list[float]:
    targets: list[float] = []
    t = 0.0
    while t < total:
        targets.append(t)
        t += spacing_m
    targets.append(total)
    return targets


def _linear_resample(dist_m: list[float], series: list[float], targets: list[float]) -> list[float]:
    """Linearly interpolate `series` (aligned with dist_m) onto `targets`,
    a non-decreasing list of cumulative distances starting at dist_m[0] or
    later. No smoothing."""
    n = len(dist_m)
    out: list[float] = []
    j = 0
    for target in targets:
        while j < n - 2 and dist_m[j + 1] < target:
            j += 1
        d0, d1 = dist_m[j], dist_m[j + 1] if j + 1 < n else dist_m[j]
        frac = 0.0 if d1 <= d0 else (target - d0) / (d1 - d0)
        frac = min(1.0, max(0.0, frac))
        v0, v1 = series[j], series[j + 1] if j + 1 < n else series[j]
        out.append(v0 + (v1 - v0) * frac)
    return out


def profile_roughness_m(dist_m: list[float], ele_m: list[float]) -> float:
    """A profile's own smoothness, independent of its source's point density:
    the RMS second difference of elevation linearly resampled onto an even
    RESAMPLE_SPACING_M grid. Real terrain changes slope gradually over many
    grid steps and leaves a small value; GPS/DEM jitter reverses direction
    almost every step and leaves a large one.
    """
    if len(dist_m) < 3:
        return 0.0
    origin = dist_m[0]
    total = dist_m[-1] - origin
    if total <= 0:
        return 0.0
    shifted = [d - origin for d in dist_m]
    targets = _even_targets(total, RESAMPLE_SPACING_M)
    grid_ele = _linear_resample(shifted, ele_m, targets)
    if len(grid_ele) < 3:
        return 0.0
    second_diffs = [grid_ele[i + 1] - 2 * grid_ele[i] + grid_ele[i - 1] for i in range(1, len(grid_ele) - 1)]
    n = len(second_diffs)
    mean = sum(second_diffs) / n
    variance = sum((x - mean) ** 2 for x in second_diffs) / n
    return math.sqrt(variance)


def smoothing_half_window_for(roughness_m: float) -> float:
    """Smoothing half-window for a profile of the given roughness: a linear
    ramp from none (smooth) to ELEVATION_SMOOTHING_HALF_WINDOW_M (noisy)."""
    if roughness_m <= ROUGHNESS_SMOOTH_M:
        return 0.0
    if roughness_m >= ROUGHNESS_NOISY_M:
        return ELEVATION_SMOOTHING_HALF_WINDOW_M
    frac = (roughness_m - ROUGHNESS_SMOOTH_M) / (ROUGHNESS_NOISY_M - ROUGHNESS_SMOOTH_M)
    return frac * ELEVATION_SMOOTHING_HALF_WINDOW_M


def roughness_smoothed(dist_m: list[float], elevations: list[float]) -> tuple[list[float], float]:
    """A profile smoothed with the window its own roughness calls for; returns
    the smoothed series and the half-window used. Every elevation source
    (route files, terrain-model drapes, network paths) goes through this, so
    the same profile gets the same gain wherever it is measured."""
    half_window_m = smoothing_half_window_for(profile_roughness_m(dist_m, elevations))
    return smoothed_elevations(elevations, dist_m, half_window_m), half_window_m


def resample_points(
    points: list[GpxPoint],
    dist_m: list[float],
    spacing_m: float = RESAMPLE_SPACING_M,
    half_window_m: float = ELEVATION_SMOOTHING_HALF_WINDOW_M,
) -> list[tuple[float, float, float, float | None]]:
    """Resample onto an even ~spacing_m grid along cumulative distance,
    linearly interpolating lat/lng and (when present) a smoothed elevation
    series (smoothed over +/-half_window_m -- see smoothed_elevations).
    Returns (lat, lng, dist_m, ele_m) tuples; the first and last raw points
    are always included.
    """
    total = dist_m[-1]
    if total <= 0:
        lat, lng, ele = points[0]
        return [(lat, lng, 0.0, ele)]

    elevations = [p[2] for p in points]
    has_ele = all(e is not None for e in elevations)
    smoothed = smoothed_elevations([e for e in elevations if e is not None], dist_m, half_window_m) if has_ele else []

    targets = _even_targets(total, spacing_m)

    lats = _linear_resample(dist_m, [p[0] for p in points], targets)
    lngs = _linear_resample(dist_m, [p[1] for p in points], targets)
    eles: list[float | None] = list(_linear_resample(dist_m, smoothed, targets)) if has_ele else [None] * len(targets)
    return list(zip(lats, lngs, targets, eles))


def bbox(points: list[GpxPoint]) -> tuple[float, float, float, float]:
    """(south, west, north, east)."""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return (min(lats), min(lngs), max(lats), max(lngs))


def _turning_point_indices(ele: list[float], noise_m: float) -> list[int]:
    """Indices of ele forming the smallest set of alternating peaks/troughs
    that reproduce gain_loss_m's hysteresis: the same reversal-from-the-
    running-extreme test, but recording *where* each confirmed extreme was
    instead of only accumulating its magnitude."""
    n = len(ele)
    if n < 2:
        return list(range(n))
    turning = [0]
    direction = 0  # 0 unknown, 1 climbing toward extreme_idx, -1 descending
    extreme_idx = 0
    for i in range(1, n):
        if direction >= 0 and ele[i] >= ele[extreme_idx]:
            extreme_idx = i
            direction = 1
        elif direction <= 0 and ele[i] <= ele[extreme_idx]:
            extreme_idx = i
            direction = -1
        else:
            reversal = ele[extreme_idx] - ele[i] if direction == 1 else ele[i] - ele[extreme_idx]
            if reversal >= noise_m:
                turning.append(extreme_idx)
                extreme_idx = i
                direction = -direction
    if turning[-1] != extreme_idx:
        turning.append(extreme_idx)
    if turning[-1] != n - 1:
        turning.append(n - 1)
    return turning


def segment_climbs(
    dist_m: list[float],
    ele_m: list[float],
    min_gain_m: float = CLIMB_MIN_GAIN_M,
    noise_m: float = CLIMB_SEGMENTATION_NOISE_M,
) -> list[ClimbSegment]:
    """Sustained climbs and descents: pair up consecutive turning points
    from the (already smoothed) elevation series and keep only pairs whose
    |elevation change| clears min_gain_m. Ordered along the route."""
    if len(ele_m) < 2:
        return []
    turning = _turning_point_indices(ele_m, noise_m)
    segments: list[ClimbSegment] = []
    for a, b in zip(turning, turning[1:]):
        delta = ele_m[b] - ele_m[a]
        magnitude = abs(delta)
        if magnitude < min_gain_m:
            continue
        seg_dist = dist_m[b] - dist_m[a]
        segments.append(
            {
                "kind": "climb" if delta > 0 else "descent",
                "start_m": dist_m[a],
                "end_m": dist_m[b],
                "gain_m": magnitude,
                "avg_grade": (delta / seg_dist) if seg_dist > 0 else 0.0,
            }
        )
    return segments


def grade_band_histogram(dist_m: list[float], ele_m: list[float]) -> dict[str, float]:
    """Share of distance (0..1, summing to ~1.0) falling in each GRADE_BANDS
    bucket, computed point-to-point over the (already smoothed) profile."""
    totals = {label: 0.0 for _, _, label in GRADE_BANDS}
    total_dist = 0.0
    for i in range(len(dist_m) - 1):
        seg = dist_m[i + 1] - dist_m[i]
        if seg <= 0:
            continue
        grade = (ele_m[i + 1] - ele_m[i]) / seg
        total_dist += seg
        for lo, hi, label in GRADE_BANDS:
            if lo <= grade < hi:
                totals[label] += seg
                break
        else:
            totals[GRADE_BANDS[-1][2]] += seg
    if total_dist <= 0:
        return {label: 0.0 for label in totals}
    return {label: v / total_dist for label, v in totals.items()}
