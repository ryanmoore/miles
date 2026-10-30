"""The athlete's own per-grade running performance, built from raw run
streams, independent of any map data. Segments (terrain_segments) are
rebuildable from the raw stream files alone, gated by model_version_tag()
like ROUTE_DERIVE_VERSION gates routes.py.

Grade comes from elevation tiles draped on each segment's lat/lng where they
cover it, else from the activity's recorded altitude; ele_source records
which. Tile fetching for history happens only in the terrain backfill job,
never on a request or startup path.
"""

import bisect
import copy
import json
import math
import logging
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Mapping, Sequence, cast
from typing_extensions import TypedDict

import numpy as np
import pandas as pd

from . import db, elevation, gpx_parse, races, streams
from .errors import describe_error
from . import routes as routes_service

_logger = logging.getLogger(__name__)

# Bump whenever a change alters what rebuild_terrain computes from the same
# cached streams; it gates a full resegmentation.
TERRAIN_MODEL_VERSION = 6


def model_version_tag() -> str:
    """Version stamp written to meta.terrain_model_version. Folds in
    streams.STREAM_DERIVE_VERSION so a stream-parsing change also forces a
    full resegmentation from the raw files on disk."""
    return f"{TERRAIN_MODEL_VERSION}.{streams.STREAM_DERIVE_VERSION}"

def sport_filter() -> tuple[str, list[str]]:
    """A WHERE condition (and its parameters) selecting the activities the
    terrain model covers. A virtual run's "terrain" is whatever the app
    simulated, not ground the athlete ran on."""
    return races.run_sport_filter(exclude=("VirtualRun",))


TERRAIN_SPORT_TYPES = tuple(sport_filter()[1])

# Stored as a small int: the row count of a multi-year history makes a string costly.
ELE_SOURCE_RECORDED = 0
ELE_SOURCE_DEM = 1

WALK_THRESHOLD_SPM = 130.0
# Used only when the cadence stream is missing outright.
WALK_SPEED_FALLBACK_MPS = 2.2
ROLLING_WINDOW_DAYS = 90

# Grade bins are numbered 0..N_GRADE_BINS-1: bin 0 holds everything below
# GRADE_BIN_MIN_PCT, the last bin everything from GRADE_BIN_MAX_PCT up, and
# each bin between spans GRADE_BIN_WIDTH_PCT.
GRADE_BIN_WIDTH_PCT = 2.0
GRADE_BIN_MIN_PCT = -30.0
GRADE_BIN_MAX_PCT = 30.0
N_GRADE_BINS = int((GRADE_BIN_MAX_PCT - GRADE_BIN_MIN_PCT) / GRADE_BIN_WIDTH_PCT) + 2

# Minimum samples before a grade bin's stats are trusted over the default.
MIN_SAMPLES_FOR_BIN = 5
# Below this a fatigue/temperature band applies no adjustment (multiplier 1.0).
MIN_SAMPLES_FOR_BAND = 20
# Tolerance, in m/s, so the descent-comfort scan doesn't stop on sample noise.
_DESCENT_NOISE_MPS = 0.05

# Elapsed-time fatigue bands, in seconds: 0-30/30-60/60-90/90-120/120min+.
FATIGUE_BAND_EDGES_S = [0.0, 1800.0, 3600.0, 5400.0, 7200.0, 86400.0]
# Temperature bands, in Celsius.
TEMP_BAND_EDGES_C = [-20.0, 0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0]
# The "no adjustment" temperature, as the first fatigue band is for fatigue.
REFERENCE_TEMP_C = 12.0

# Generic defaults, used until the athlete has enough of their own data.
_DEFAULT_FLAT_SPEED_MPS = 3.0  # ~9:00/mi
_DEFAULT_WALK_ONSET_PCT = 15.0
_DEFAULT_DESCENT_COMFORT_PCT = -12.0

Confidence = Literal["low", "medium", "high"]


class TerrainDataError(ValueError):
    """Raised when an activity has no cached streams, or streams with no
    usable elevation, for a terrain calculation that needs them."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def init_terrain_schema(conn: sqlite3.Connection) -> None:
    # Values are compact integers (tenths of a percent/degree, cm/s, whole
    # seconds) rather than REAL: far smaller per row at multi-year row counts,
    # and precise enough. WITHOUT ROWID because (activity_id, idx) is already
    # a unique clustering key.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terrain_segments (
            activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
            idx         INTEGER NOT NULL,
            dist_m      REAL NOT NULL,
            elapsed_s   INTEGER,
            grade_dpct  INTEGER NOT NULL,
            speed_cms   INTEGER,
            heartrate   INTEGER,
            cadence_spm INTEGER,
            is_walking  INTEGER NOT NULL,
            temp_dc     INTEGER,
            ele_source  INTEGER NOT NULL,
            PRIMARY KEY (activity_id, idx)
        ) WITHOUT ROWID
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_terrain_segments_grade ON terrain_segments(grade_dpct)")
    # One row per processed activity, holding the date window queries filter
    # on so it isn't repeated per segment. An activity with no segments still
    # gets a row (segment_count 0) so it is processed once per model version.
    # needs_dem marks a segmentation done without fetching elevation tiles in
    # which some segments fell back to recorded altitude; the backfill
    # re-segments those with fetching on.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terrain_segment_dates (
            activity_id   INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            start_date    TEXT NOT NULL,
            segment_count INTEGER NOT NULL DEFAULT 1,
            needs_dem     INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_terrain_segment_dates_date ON terrain_segment_dates(start_date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terrain_snapshots (
            snapshot_id               INTEGER PRIMARY KEY,
            as_of                     TEXT NOT NULL,
            window_days               INTEGER NOT NULL,
            walk_onset_grade_pct      REAL NOT NULL,
            descent_comfort_grade_pct REAL NOT NULL,
            confidence                TEXT NOT NULL,
            bins_json                 TEXT NOT NULL,
            created_at                TEXT NOT NULL,
            UNIQUE (as_of, window_days)
        )
    """)
    # The resumable backfill's state: the queue is the resume point.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terrain_backfill_queue (
            activity_id INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            start_date  TEXT,
            queued_at   TEXT NOT NULL
        )
    """)
    # An activity the backfill couldn't fetch or segment. A 404 is permanent
    # and keeps it out of the queue; any other failure only records why the
    # last attempt failed. model_version is set only when segmenting a
    # readable stream file failed: not retried until model_version_tag() changes.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS terrain_backfill_failures (
            activity_id   INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            status_code   INTEGER,
            error         TEXT NOT NULL,
            failed_at     TEXT NOT NULL,
            model_version TEXT
        )
    """)
    conn.commit()


class TerrainRebuildingError(RuntimeError):
    """The terrain model is being rebuilt after a version bump; its tables are
    not trustworthy until that finishes."""


def connect() -> sqlite3.Connection:
    """Connection for terrain endpoints/tools. When the stored model version is
    behind, starts the rebuild on a background thread and raises
    TerrainRebuildingError rather than making the caller wait. A database with
    nothing to rebuild is simply stamped."""
    conn = db.connect()
    if model_is_current(conn):
        return conn
    if not _has_terrain_activities(conn):
        ensure_terrain_derived(conn)
        return conn
    conn.close()
    start_background_rebuild()
    raise TerrainRebuildingError("the terrain model is being rebuilt after an update; try again in a few minutes")


# ---------------------------------------------------------------------------
# Grade bins
# ---------------------------------------------------------------------------


def grade_bin(grade_pct: float) -> int:
    if grade_pct < GRADE_BIN_MIN_PCT:
        return 0
    if grade_pct >= GRADE_BIN_MAX_PCT:
        return N_GRADE_BINS - 1
    return 1 + math.floor((grade_pct - GRADE_BIN_MIN_PCT) / GRADE_BIN_WIDTH_PCT)


def _bin_bounds(index: int) -> tuple[float, float]:
    """(lo, hi) percent of a bin; the first has no lower bound, the last no
    upper bound."""
    lo = -math.inf if index == 0 else GRADE_BIN_MIN_PCT + (index - 1) * GRADE_BIN_WIDTH_PCT
    hi = math.inf if index == N_GRADE_BINS - 1 else GRADE_BIN_MIN_PCT + index * GRADE_BIN_WIDTH_PCT
    return lo, hi


def bin_center(index: int) -> float:
    lo, hi = _bin_bounds(index)
    if math.isinf(lo):
        return hi - GRADE_BIN_WIDTH_PCT / 2
    if math.isinf(hi):
        return lo + GRADE_BIN_WIDTH_PCT / 2
    return (lo + hi) / 2


def _json_bound(pct: float) -> float | None:
    return None if math.isinf(pct) else pct


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _band_multiplier(bands: Sequence[tuple[float, float, float]], x: float) -> float:
    for lo, hi, mult in bands:
        if lo <= x < hi:
            return mult
    return 1.0


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class GradeBinStats:
    index: int
    median_speed_mps: float | None
    efficiency_median: float | None
    walk_fraction: float | None
    sample_count: int

    @property
    def lo_pct(self) -> float:
        return _bin_bounds(self.index)[0]

    @property
    def hi_pct(self) -> float:
        return _bin_bounds(self.index)[1]

    def to_dict(self) -> dict:
        """The bin as the API and snapshots report it: bounds in percent, null
        where the bin is open-ended."""
        return {
            "lo_pct": _json_bound(self.lo_pct),
            "hi_pct": _json_bound(self.hi_pct),
            "median_speed_mps": self.median_speed_mps,
            "efficiency_median": self.efficiency_median,
            "walk_fraction": self.walk_fraction,
            "sample_count": self.sample_count,
        }


@dataclass
class TerrainModel:
    as_of: str
    window_days: int
    walk_onset_grade_pct: float
    descent_comfort_grade_pct: float  # negative
    confidence: Confidence
    bins: list[GradeBinStats] = field(default_factory=list)
    # (lo, hi, multiplier) bands, relative to the reference band.
    fatigue_bands: list[tuple[float, float, float]] = field(default_factory=list)
    temp_bands: list[tuple[float, float, float]] = field(default_factory=list)

    def classify_grade(self, grade_pct: float) -> Literal["run", "grind", "hike"]:
        if grade_pct >= self.walk_onset_grade_pct:
            return "hike"
        if grade_pct <= self.descent_comfort_grade_pct:
            return "grind"
        if grade_pct >= self.walk_onset_grade_pct / 2.0:
            return "grind"
        return "run"

    def expected_speed_mps(self, grade_pct: float, elapsed_s: float = 0.0, temp_c: float | None = None) -> float:
        median_speed = self.bins[grade_bin(grade_pct)].median_speed_mps
        if median_speed is not None:
            base = median_speed
        else:
            base = _DEFAULT_FLAT_SPEED_MPS / streams.minetti_cost_ratio(grade_pct)
        fatigue = _band_multiplier(self.fatigue_bands, elapsed_s)
        temp = 1.0 if temp_c is None else _band_multiplier(self.temp_bands, temp_c)
        return base * fatigue * temp

    def score_profile(self, grades_pct: Sequence[float], segment_m: float = 20.0) -> dict[str, float]:
        if not grades_pct:
            return {"run_pct": 0.0, "grind_pct": 0.0, "hike_pct": 0.0, "longest_hike_m": 0.0, "steep_descent_m": 0.0}
        counts = {"run": 0, "grind": 0, "hike": 0}
        longest_hike = 0.0
        current_hike = 0.0
        steep_descent = 0.0
        for g in grades_pct:
            cls = self.classify_grade(g)
            counts[cls] += 1
            if cls == "hike":
                current_hike += segment_m
                longest_hike = max(longest_hike, current_hike)
            else:
                current_hike = 0.0
            if g <= self.descent_comfort_grade_pct:
                steep_descent += segment_m
        total = len(grades_pct)
        return {
            "run_pct": 100.0 * counts["run"] / total,
            "grind_pct": 100.0 * counts["grind"] / total,
            "hike_pct": 100.0 * counts["hike"] / total,
            "longest_hike_m": longest_hike,
            "steep_descent_m": steep_descent,
        }


def default_model() -> TerrainModel:
    """Generic defaults for an athlete with no terrain history: speed per grade
    follows the Minetti cost curve from a ~9:00/mi flat pace."""
    bins: list[GradeBinStats] = []
    for index in range(N_GRADE_BINS):
        center = bin_center(index)
        speed = _DEFAULT_FLAT_SPEED_MPS / streams.minetti_cost_ratio(center)
        walk_fraction = 0.0 if center < _DEFAULT_WALK_ONSET_PCT else 1.0
        bins.append(GradeBinStats(index, speed, None, walk_fraction, 0))
    neutral_fatigue = [(lo, hi, 1.0) for lo, hi in zip(FATIGUE_BAND_EDGES_S, FATIGUE_BAND_EDGES_S[1:])]
    neutral_temp = [(lo, hi, 1.0) for lo, hi in zip(TEMP_BAND_EDGES_C, TEMP_BAND_EDGES_C[1:])]
    return TerrainModel(
        as_of=date.today().isoformat(),
        window_days=ROLLING_WINDOW_DAYS,
        walk_onset_grade_pct=_DEFAULT_WALK_ONSET_PCT,
        descent_comfort_grade_pct=_DEFAULT_DESCENT_COMFORT_PCT,
        confidence="low",
        bins=bins,
        fatigue_bands=neutral_fatigue,
        temp_bands=neutral_temp,
    )


def _derive_walk_onset(bins: list[GradeBinStats], fallback: float) -> float:
    candidates = sorted(
        (b for b in bins if b.lo_pct >= 0 and b.sample_count > 0 and b.walk_fraction is not None),
        key=lambda b: b.lo_pct,
    )
    for b in candidates:
        if cast(float, b.walk_fraction) >= 0.5:
            return _clip(b.lo_pct, 5.0, 35.0)
    return fallback


def _derive_descent_comfort(bins: list[GradeBinStats], fallback: float) -> float:
    """Scan from flat toward steeper descents; the comfortable grade is the
    steepest reached before median speed stops rising."""
    candidates = sorted(
        (b for b in bins if b.hi_pct <= 0 and b.sample_count > 0 and b.median_speed_mps is not None),
        key=lambda b: -b.lo_pct,
    )
    best_lo: float | None = None
    best_speed = float("-inf")
    for b in candidates:
        speed = cast(float, b.median_speed_mps)
        if speed + _DESCENT_NOISE_MPS < best_speed:
            break
        best_speed = max(best_speed, speed)
        best_lo = b.lo_pct
    return _clip(best_lo, -35.0, -2.0) if best_lo is not None else fallback


def _confidence_for(total_real_samples: int) -> Confidence:
    if total_real_samples >= 5000:
        return "high"
    if total_real_samples >= 500:
        return "medium"
    return "low"


def _derive_bands(
    df: pd.DataFrame, key_col: str, edges: list[float], reference_value: float
) -> list[tuple[float, float, float]]:
    """Median grade-adjusted speed in each band of key_col relative to the band
    containing reference_value, so a low multiplier is a fatigue/temperature
    effect, not hills."""
    bands = list(zip(edges, edges[1:]))
    neutral = [(lo, hi, 1.0) for lo, hi in bands]
    if df.empty:
        return neutral
    values = df[key_col]
    ref_lo, ref_hi = next(((lo, hi) for lo, hi in bands if lo <= reference_value < hi), bands[0])
    ref_sub = df.loc[(values >= ref_lo) & (values < ref_hi), "gap_speed_mps"]
    if len(ref_sub) < MIN_SAMPLES_FOR_BAND:
        return neutral
    ref_median = float(ref_sub.median())
    if ref_median <= 0:
        return neutral
    out: list[tuple[float, float, float]] = []
    for lo, hi in bands:
        sub = df.loc[(values >= lo) & (values < hi), "gap_speed_mps"]
        if len(sub) < MIN_SAMPLES_FOR_BAND:
            out.append((lo, hi, 1.0))
            continue
        out.append((lo, hi, float(sub.median() / ref_median)))
    return out


_MODEL_COLUMNS = ["grade_pct", "speed_mps", "heartrate", "cadence_spm", "is_walking", "elapsed_s", "temp_c"]
_MODEL_CACHE_SIZE = 8
# Backstop for what the marker cannot see: another process re-segmenting an
# activity in place, or a write not yet committed when the model was read.
_MODEL_CACHE_TTL_S = 60.0
_model_cache: OrderedDict[tuple, tuple[float, TerrainModel]] = OrderedDict()
_model_cache_lock = threading.Lock()
_segment_generation = 0


def _grade_bins(grades_pct: pd.Series) -> np.ndarray:
    """grade_bin over a column."""
    inner = 1 + np.floor((grades_pct.to_numpy(dtype=float) - GRADE_BIN_MIN_PCT) / GRADE_BIN_WIDTH_PCT)
    inner = np.where(grades_pct < GRADE_BIN_MIN_PCT, 0, inner)
    return np.where(grades_pct >= GRADE_BIN_MAX_PCT, N_GRADE_BINS - 1, inner).astype(int)


def _bump_segment_generation() -> None:
    global _segment_generation
    with _model_cache_lock:
        _segment_generation += 1


def _data_marker(conn: sqlite3.Connection) -> tuple:
    """What the segment data looks like now, from the per-activity dates table
    (every writer replaces an activity's dates row, so its count, sums, and
    needs_dem flags move with additions, deletions, and date or flag changes
    from any process), plus this process's write generation for a replacement
    that leaves all of those equal, and the activities whose HR is flagged."""
    row = conn.execute(
        "SELECT count(*), total(segment_count), total(needs_dem), total(activity_id), total(julianday(start_date)), "
        f"(SELECT count(*) || ':' || total(activity_id) FROM activities WHERE NOT ({db.hr_trusted_sql()})) "
        "FROM terrain_segment_dates"
    ).fetchone()
    return (_segment_generation, *tuple(row))


def current_model(
    conn: sqlite3.Connection, window_days: int = ROLLING_WINDOW_DAYS, as_of: date | None = None
) -> TerrainModel:
    """The athlete's model over a trailing window; bins with fewer than
    MIN_SAMPLES_FOR_BIN samples use default_model()'s. Memoized per window,
    date, model version, and the database's segment-change marker; the caller
    gets its own copy."""
    as_of = as_of or date.today()
    key = (_data_marker(conn), as_of.isoformat(), window_days, TERRAIN_MODEL_VERSION)
    with _model_cache_lock:
        now = time.monotonic()
        entry = _model_cache.get(key)
        if entry is None or now - entry[0] > _MODEL_CACHE_TTL_S:
            # Building under the lock makes concurrent first requests compute once.
            entry = (now, _build_model(conn, window_days, as_of))
            _model_cache[key] = entry
            while len(_model_cache) > _MODEL_CACHE_SIZE:
                _model_cache.popitem(last=False)
        else:
            _model_cache.move_to_end(key)
        return copy.deepcopy(entry[1])


def _build_model(conn: sqlite3.Connection, window_days: int, as_of: date) -> TerrainModel:
    start_date = (as_of - timedelta(days=window_days)).isoformat()
    end_date = as_of.isoformat()
    cursor = conn.execute(
        f"""
        SELECT
            ts.grade_dpct / 10.0 AS grade_pct, ts.speed_cms / 100.0 AS speed_mps,
            {db.hr_sql("ts.heartrate", "a")} AS heartrate,
            ts.cadence_spm, ts.is_walking, ts.elapsed_s, ts.temp_dc / 10.0 AS temp_c
        FROM terrain_segments ts
        JOIN terrain_segment_dates d ON d.activity_id = ts.activity_id
        LEFT JOIN activities a ON a.activity_id = ts.activity_id
        WHERE d.start_date >= ? AND d.start_date <= ?
        """,
        [start_date, end_date],
    )
    cursor.row_factory = None  # plain tuples: building a dict per row dominates the fetch
    rows = cursor.fetchall()

    defaults = default_model()
    defaults.as_of = as_of.isoformat()
    defaults.window_days = window_days
    if not rows:
        return defaults

    df = pd.DataFrame.from_records(rows, columns=_MODEL_COLUMNS)
    df["gap_speed_mps"] = df["speed_mps"] * streams.minetti_cost_ratios(cast(pd.Series, df["grade_pct"]))
    df["bin"] = _grade_bins(cast(pd.Series, df["grade_pct"]))
    by_bin: dict[int, pd.DataFrame] = {cast(int, index): group for index, group in df.groupby("bin")}

    bins: list[GradeBinStats] = []
    total_real_samples = 0
    for default in defaults.bins:
        sub = by_bin.get(default.index)
        if sub is None or len(sub) < MIN_SAMPLES_FOR_BIN:
            bins.append(default)
            continue
        n = len(sub)
        total_real_samples += n
        speeds = cast(pd.Series, sub["speed_mps"]).dropna()
        median_speed = float(cast(float, speeds.median())) if len(speeds) else default.median_speed_mps
        with_heartrate = cast(pd.DataFrame, sub[cast(pd.Series, sub["heartrate"]) > 0])
        efficiency = (
            float(cast(float, cast(pd.Series, with_heartrate["gap_speed_mps"] / with_heartrate["heartrate"]).median()))
            if len(with_heartrate) else None
        )
        walk_fraction = float(cast(float, cast(pd.Series, sub["is_walking"]).mean()))
        bins.append(GradeBinStats(default.index, median_speed, efficiency, walk_fraction, n))

    walk_onset = _derive_walk_onset(bins, defaults.walk_onset_grade_pct)
    descent_comfort = _derive_descent_comfort(bins, defaults.descent_comfort_grade_pct)
    confidence = _confidence_for(total_real_samples)

    is_running = df["is_walking"] == 0
    has_gap_speed = cast(pd.Series, df["gap_speed_mps"]).notna()
    run_df = cast(pd.DataFrame, df[is_running & has_gap_speed])
    fatigue_bands = _derive_bands(run_df.dropna(subset=["elapsed_s"]), "elapsed_s", FATIGUE_BAND_EDGES_S, 0.0)
    temp_bands = _derive_bands(run_df.dropna(subset=["temp_c"]), "temp_c", TEMP_BAND_EDGES_C, REFERENCE_TEMP_C)

    return TerrainModel(
        as_of=as_of.isoformat(),
        window_days=window_days,
        walk_onset_grade_pct=walk_onset,
        descent_comfort_grade_pct=descent_comfort,
        confidence=confidence,
        bins=bins,
        fatigue_bands=fatigue_bands,
        temp_bands=temp_bands,
    )


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def _present(x: object) -> bool:
    """True unless x is None or a float NaN; narrows a Series scalar that
    pandas's notna() overloads can't."""
    if x is None:
        return False
    return not (isinstance(x, float) and math.isnan(x))


def _flt(x: object) -> float | None:
    return float(cast(float, x)) if _present(x) else None


def _nearest(raw_dist: list[float], raw_series: pd.Series, targets: list[float]) -> list[object]:
    """Value of raw_series at the sample nearest each target distance, so
    speed/HR/cadence keep recorded values instead of an interpolated blend."""
    n = len(raw_dist)
    out: list[object] = []
    for t in targets:
        i = bisect.bisect_left(raw_dist, t)
        if i <= 0:
            idx = 0
        elif i >= n:
            idx = n - 1
        else:
            idx = i - 1 if (t - raw_dist[i - 1]) <= (raw_dist[i] - t) else i
        out.append(raw_series.iloc[idx])
    return out


class _AltitudeResample(TypedDict):
    dist_m: list[float]
    ele_m: list[float]
    lat: list[float]
    lng: list[float]


def _resample_altitude(df: pd.DataFrame) -> _AltitudeResample | None:
    """Resample distance/altitude/position onto the 20 m grid routes use
    (gpx_parse.resample_points also smooths altitude). None when there is no
    usable distance/altitude series."""
    sub = df.dropna(subset=["distance_m", "altitude_m"])
    if len(sub) < 2:
        return None
    dist = [float(d) for d in sub["distance_m"].tolist()]
    if dist[-1] <= 0 or any(b < a for a, b in zip(dist, dist[1:])):
        return None
    alt = [float(a) for a in sub["altitude_m"].tolist()]
    lat_col = sub["lat"] if "lat" in sub.columns else pd.Series([None] * len(sub))
    lng_col = sub["lng"] if "lng" in sub.columns else pd.Series([None] * len(sub))
    # A missing fix stays NaN (it propagates through interpolation) rather
    # than becoming a coordinate: _drape_activity_dem skips non-finite points.
    points: list[gpx_parse.GpxPoint] = [
        (float(la) if pd.notna(la) else math.nan, float(ln) if pd.notna(ln) else math.nan, a)
        for la, ln, a in zip(lat_col.tolist(), lng_col.tolist(), alt)
    ]
    resampled = gpx_parse.resample_points(points, dist)
    return {
        "dist_m": [p[2] for p in resampled],
        "ele_m": cast(list[float], [p[3] for p in resampled]),
        "lat": [p[0] for p in resampled],
        "lng": [p[1] for p in resampled],
    }


def _drape_activity_dem(
    dem_sampler: elevation.ElevationSampler | None, dist_m: list[float], lat: list[float], lng: list[float]
) -> list[float | None]:
    """Tile elevation at every resampled point, or all-None with no sampler.
    A fully covered drape is roughness-smoothed; a partial one is returned
    raw, and _segment_activity falls back to recorded altitude where a point
    is None. Points without a GPS fix are never sampled."""
    if dem_sampler is None:
        return [None] * len(dist_m)
    located = [i for i, (la, ln) in enumerate(zip(lat, lng)) if math.isfinite(la) and math.isfinite(ln)]
    if not located:
        return [None] * len(dist_m)
    sampled = dem_sampler.sample([(lat[i], lng[i]) for i in located])
    raw: list[float | None] = [None] * len(dist_m)
    for i, value in zip(located, sampled):
        raw[i] = value
    if all(v is not None for v in raw):
        values = cast(list[float], raw)
        return cast(list[float | None], gpx_parse.roughness_smoothed(dist_m, values)[0])
    return raw


class _SegmentValues(TypedDict):
    dist_m: float
    elapsed_s: float | None
    grade_pct: float
    speed_mps: float | None
    heartrate: float | None
    cadence_spm: float | None
    is_walking: bool
    ele_source: int


def _segment_activity(
    df: pd.DataFrame, walk_threshold_spm: float, *, dem_sampler: elevation.ElevationSampler | None = None
) -> list[_SegmentValues] | None:
    resampled = _resample_altitude(df)
    if resampled is None:
        return None
    dist_out, ele_out = resampled["dist_m"], resampled["ele_m"]
    if len(dist_out) < 2:
        return None
    dem_out = _drape_activity_dem(dem_sampler, dist_out, resampled["lat"], resampled["lng"])

    sub = cast(pd.DataFrame, df.dropna(subset=["distance_m"]))
    raw_dist = [float(d) for d in sub["distance_m"].tolist()]
    starts = dist_out[:-1]

    speed_raw = _nearest(raw_dist, cast(pd.Series, sub["velocity_smooth_mps"]), starts)
    heartrate_raw = _nearest(raw_dist, cast(pd.Series, sub["heartrate"]), starts)
    cadence_raw = _nearest(raw_dist, cast(pd.Series, sub["cadence"]), starts)
    elapsed_raw = _nearest(raw_dist, cast(pd.Series, sub["time_s"]), starts)
    moving_raw = _nearest(raw_dist, cast(pd.Series, sub["moving"]), starts)

    segments: list[_SegmentValues] = []
    for i, start in enumerate(starts):
        moving = moving_raw[i]
        if _present(moving) and not bool(moving):
            continue
        seg_len = dist_out[i + 1] - dist_out[i]
        if seg_len <= 0:
            continue
        # Mixing a modeled and a recorded endpoint would invent a grade
        # neither source measured, so tiles must cover both.
        dem_a, dem_b = dem_out[i], dem_out[i + 1]
        if dem_a is not None and dem_b is not None:
            rise_m, ele_source = dem_b - dem_a, ELE_SOURCE_DEM
        else:
            rise_m, ele_source = ele_out[i + 1] - ele_out[i], ELE_SOURCE_RECORDED
        grade_pct = rise_m / seg_len * 100.0
        speed = _flt(speed_raw[i])
        heartrate = _flt(heartrate_raw[i])
        # Strava's running cadence stream counts one foot only.
        cadence_raw_val = _flt(cadence_raw[i])
        cadence_spm = cadence_raw_val * 2.0 if cadence_raw_val is not None else None
        if cadence_spm is not None:
            is_walking = cadence_spm < walk_threshold_spm
        elif speed is not None:
            is_walking = speed < WALK_SPEED_FALLBACK_MPS
        else:
            is_walking = False
        segments.append({
            "dist_m": start,
            "elapsed_s": _flt(elapsed_raw[i]),
            "grade_pct": grade_pct,
            "speed_mps": speed,
            "heartrate": heartrate,
            "cadence_spm": cadence_spm,
            "is_walking": is_walking,
            "ele_source": ele_source,
        })
    return segments


def _round_or_none(value: float | None, scale: float) -> int | None:
    return round(value * scale) if value is not None else None


def segment_and_store_activity(
    conn: sqlite3.Connection,
    activity_id: int,
    df: pd.DataFrame,
    start_date: str,
    temp_c: float | None,
    walk_threshold_spm: float,
    *,
    dem_sampler: elevation.ElevationSampler | None = None,
    dem_fetch: bool = False,
) -> int:
    """(Re)segment one activity's streams, replacing its terrain_segments rows
    and terrain_segment_dates row. Returns the segments stored (0 without
    usable elevation; the dates row still records the attempt).
    The sampler is cache-only. With dem_fetch False, a segment that fell back
    to recorded altitude flags the activity needs_dem; the backfill fetches
    tiles first and passes dem_fetch=True, after which a fallback is final."""
    segments = _segment_activity(df, walk_threshold_spm, dem_sampler=dem_sampler)
    conn.execute("DELETE FROM terrain_segments WHERE activity_id = ?", [activity_id])
    conn.execute("DELETE FROM terrain_segment_dates WHERE activity_id = ?", [activity_id])
    needs_dem = bool(segments) and not dem_fetch and any(s["ele_source"] == ELE_SOURCE_RECORDED for s in segments)
    conn.execute(
        "INSERT INTO terrain_segment_dates (activity_id, start_date, segment_count, needs_dem) VALUES (?, ?, ?, ?)",
        [activity_id, start_date, len(segments or []), int(needs_dem)],
    )
    if not segments:
        _bump_segment_generation()
        return 0
    conn.executemany(
        """
        INSERT INTO terrain_segments (
            activity_id, idx, dist_m, elapsed_s, grade_dpct,
            speed_cms, heartrate, cadence_spm, is_walking, temp_dc, ele_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                activity_id, i, s["dist_m"], _round_or_none(s["elapsed_s"], 1.0), _round_or_none(s["grade_pct"], 10.0),
                _round_or_none(s["speed_mps"], 100.0), _round_or_none(s["heartrate"], 1.0),
                _round_or_none(s["cadence_spm"], 1.0), int(s["is_walking"]),
                _round_or_none(temp_c, 10.0), s["ele_source"],
            )
            for i, s in enumerate(segments)
        ],
    )
    _bump_segment_generation()
    return len(segments)


def activity_temp_c(conn: sqlite3.Connection, activity_id: int, stream_df: pd.DataFrame | None = None) -> float | None:
    """Weather temperature for the activity, else the mean of the device's temp
    stream, which drifts with body heat and sun."""
    row = conn.execute(
        "SELECT temp_c_avg, temp_c_start, temp_c_end FROM weather WHERE activity_id = ?", [activity_id]
    ).fetchone()
    if row is not None:
        if row["temp_c_avg"] is not None:
            return float(row["temp_c_avg"])
        vals = [v for v in (row["temp_c_start"], row["temp_c_end"]) if v is not None]
        if vals:
            return float(sum(vals) / len(vals))
    if stream_df is not None and "temp_c" in stream_df.columns:
        device = cast(pd.Series, stream_df["temp_c"]).dropna()
        if len(device):
            return float(cast(float, device.mean()))
    return None


def _stream_fixes(df: pd.DataFrame) -> list[tuple[float, float]]:
    if "lat" not in df.columns or "lng" not in df.columns:
        return []
    fixes = df[["lat", "lng"]].dropna()
    return list(zip(fixes["lat"].astype(float), fixes["lng"].astype(float)))


def segment_cached_activity(
    conn: sqlite3.Connection,
    activity_id: int,
    df: pd.DataFrame | None = None,
    *,
    fetch_dem: bool = False,
    dem_sampler: elevation.ElevationSampler | None = None,
) -> int:
    """Segment one run from its cached streams (df, if already loaded) and
    store the result. Returns the segments stored; 0 for a non-terrain sport,
    an unsynced activity, or no usable elevation. fetch_dem downloads the
    tiles the track crosses (the backfill's call to make); otherwise only
    cached tiles are read, and an activity they do not cover keeps recorded
    altitude and is flagged for a later pass. dem_sampler lets a caller
    segmenting many activities share one sampler's tile memo."""
    row = conn.execute(
        "SELECT sport_type, start_date FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    if row is None or row["sport_type"] not in TERRAIN_SPORT_TYPES:
        return 0
    stream_df = df if df is not None else load_stream_file(activity_id)
    if fetch_dem:
        elevation.ensure_tiles(conn, points=_stream_fixes(stream_df))
    if dem_sampler is None:
        dem_sampler = elevation.sampler_for(conn)
    n = segment_and_store_activity(
        conn,
        activity_id,
        stream_df,
        (row["start_date"] or "")[:10],
        activity_temp_c(conn, activity_id, stream_df),
        WALK_THRESHOLD_SPM,
        dem_sampler=dem_sampler,
        dem_fetch=fetch_dem,
    )
    conn.commit()
    return n


def segment_or_record_failure(
    conn: sqlite3.Connection,
    activity_id: int,
    raw_streams: Mapping[str, object] | None = None,
    *,
    fetch_dem: bool = False,
    dem_sampler: elevation.ElevationSampler | None = None,
) -> int | None:
    """segment_cached_activity, after storing raw_streams (just fetched from
    Strava) when given. A failure that is the activity's own is recorded
    against it, its partial rows dropped, and None returned; a database error
    or a failed tile fetch propagates, since neither is the activity's fault."""
    try:
        if raw_streams is not None:
            streams.cache_streams(activity_id, raw_streams)
        return segment_cached_activity(conn, activity_id, fetch_dem=fetch_dem, dem_sampler=dem_sampler)
    except (sqlite3.Error, elevation.ElevationFetchError):
        raise
    except Exception as e:
        _logger.warning("terrain segmentation failed for activity %s", activity_id, exc_info=True)
        for table in ("terrain_segments", "terrain_segment_dates"):
            conn.execute(f"DELETE FROM {table} WHERE activity_id = ?", [activity_id])
        _bump_segment_generation()
        record_segmentation_failure(conn, activity_id, e)
        return None


def activities_to_segment(conn: sqlite3.Connection) -> list[int]:
    """Terrain-model activities with streams on disk that are unprocessed or
    were segmented without full tile coverage, newest first. Activities whose
    segmentation failed at this model version are left out."""
    failed = segmentation_failed_ids(conn)
    sport_sql, sport_params = sport_filter()
    rows = conn.execute(
        f"""
        SELECT activity_id FROM activities a
        LEFT JOIN terrain_segment_dates d USING (activity_id)
        WHERE {sport_sql} AND (d.activity_id IS NULL OR d.needs_dem = 1)
        ORDER BY a.start_date DESC
        """,
        sport_params,
    ).fetchall()
    return [r["activity_id"] for r in rows if r["activity_id"] not in failed and streams.has_cached_streams(r["activity_id"])]


# ---------------------------------------------------------------------------
# Rebuild / version gating
# ---------------------------------------------------------------------------


_rebuild_lock = threading.Lock()


def model_is_current(conn: sqlite3.Connection) -> bool:
    return db.get_meta(conn, "terrain_model_version") == model_version_tag()


def _has_terrain_activities(conn: sqlite3.Connection) -> bool:
    sport_sql, sport_params = sport_filter()
    return conn.execute(f"SELECT 1 FROM activities WHERE {sport_sql} LIMIT 1", sport_params).fetchone() is not None


def ensure_terrain_derived(conn: sqlite3.Connection) -> None:
    """Run the full rebuild if the stored model version is behind. Raises
    TerrainRebuildingError if another thread is already rebuilding."""
    if model_is_current(conn):
        return
    if not _rebuild_lock.acquire(blocking=False):
        raise TerrainRebuildingError("the terrain model is being rebuilt")
    started = time.monotonic()
    try:
        rebuild_terrain(conn, full=True)
    finally:
        _rebuild_lock.release()
    _logger.info("terrain model %s rebuilt in %.1f s", model_version_tag(), time.monotonic() - started)


_rebuild_thread_lock = threading.Lock()
_rebuild_thread: threading.Thread | None = None


REBUILD_ERROR_META = "terrain_rebuild_error"


def _rebuild_in_background() -> None:
    with db.connection() as conn:
        try:
            ensure_terrain_derived(conn)
            conn.execute("DELETE FROM meta WHERE key = ?", (REBUILD_ERROR_META,))
            conn.commit()
        except TerrainRebuildingError:
            pass  # another thread is already on it
        except Exception as e:
            _logger.exception("terrain model rebuild failed")
            db.set_meta(conn, REBUILD_ERROR_META, describe_error(e))
            conn.commit()


def start_background_rebuild_if_stale() -> None:
    with db.connection() as conn:
        stale = not model_is_current(conn) and _has_terrain_activities(conn)
    if stale:
        start_background_rebuild()


def start_background_rebuild() -> None:
    """Start the rebuild on a daemon thread unless one is already running."""
    global _rebuild_thread
    with _rebuild_thread_lock:
        if _rebuild_thread is not None and _rebuild_thread.is_alive():
            return
        _rebuild_thread = threading.Thread(target=_rebuild_in_background, daemon=True, name="terrain-rebuild")
        _rebuild_thread.start()



class StreamFileError(Exception):
    """The raw stream file of an activity could not be read."""


def load_stream_file(activity_id: int) -> pd.DataFrame:
    """Read an activity's cached streams; an unreadable file is discarded for
    the backfill to refetch."""
    try:
        return streams.get_stream_dataframe(activity_id)
    except Exception as e:
        streams.discard_raw_streams(activity_id)
        raise StreamFileError(f"unreadable stream file: {e}") from e


def record_failure(
    conn: sqlite3.Connection,
    activity_id: int,
    status_code: int | None,
    error: str,
    model_version: str | None = None,
) -> None:
    """Note why an activity could not be fetched or segmented (latest wins)."""
    conn.execute(
        """
        INSERT INTO terrain_backfill_failures (activity_id, status_code, error, failed_at, model_version)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(activity_id) DO UPDATE SET
            status_code = excluded.status_code, error = excluded.error, failed_at = excluded.failed_at,
            model_version = excluded.model_version
        """,
        [activity_id, status_code, error[:200], datetime.now(timezone.utc).isoformat(), model_version],
    )
    conn.commit()


def record_segmentation_failure(conn: sqlite3.Connection, activity_id: int, error: Exception) -> None:
    """Record a segmentation failure. An unreadable stream file is refetched;
    any other failure blocks retries until the model version changes."""
    versioned = None if isinstance(error, StreamFileError) else model_version_tag()
    record_failure(conn, activity_id, None, describe_error(error, None), versioned)


def segmentation_failed_ids(conn: sqlite3.Connection) -> set[int]:
    """Activities whose segmentation failed at the current model version."""
    rows = conn.execute(
        "SELECT activity_id FROM terrain_backfill_failures WHERE model_version = ?", [model_version_tag()]
    ).fetchall()
    return {r["activity_id"] for r in rows}


def rebuild_terrain(conn: sqlite3.Connection, *, full: bool = False) -> dict[str, int]:
    """Segment every run whose streams are cached and current (never touches
    Strava), then refresh the snapshots. full=True resegments every activity,
    required after a model_version_tag() bump."""
    full = full or not model_is_current(conn)

    if full:
        already_segmented: set[int] = set()
    else:
        already_segmented = {
            r["activity_id"] for r in conn.execute("SELECT activity_id FROM terrain_segment_dates").fetchall()
        }
    skipped = already_segmented | segmentation_failed_ids(conn)

    sport_sql, sport_params = sport_filter()
    for table in ("terrain_segments", "terrain_segment_dates"):
        conn.execute(
            f"DELETE FROM {table} WHERE activity_id NOT IN (SELECT activity_id FROM activities WHERE {sport_sql})", sport_params
        )
    _bump_segment_generation()
    rows = conn.execute(
        f"SELECT activity_id FROM activities WHERE {sport_sql} ORDER BY start_date DESC", sport_params
    ).fetchall()

    # Runs on request/startup paths, so it must never wait on a tile fetch.
    dem_sampler = elevation.sampler_for(conn)

    segmented = 0
    for row in rows:
        activity_id = row["activity_id"]
        if activity_id in skipped:
            continue
        if not streams.has_cached_streams(activity_id):
            continue  # backfill hasn't reached this activity yet
        # One failing activity must not keep the model stale for the rest.
        if segment_or_record_failure(conn, activity_id, dem_sampler=dem_sampler):
            segmented += 1

    db.set_meta(conn, "terrain_model_version", model_version_tag())
    conn.commit()

    if full:
        snapshots = backfill_snapshots(conn, ROLLING_WINDOW_DAYS)
    else:
        record_snapshot(conn)
        snapshots = 1
    return {"activities_segmented": segmented, "snapshots": snapshots}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def record_snapshot(conn: sqlite3.Connection, as_of: date | None = None, window_days: int = ROLLING_WINDOW_DAYS) -> None:
    as_of = as_of or date.today()
    model = current_model(conn, window_days=window_days, as_of=as_of)
    bins_payload = [b.to_dict() for b in model.bins]
    conn.execute(
        """
        INSERT INTO terrain_snapshots (
            as_of, window_days, walk_onset_grade_pct, descent_comfort_grade_pct, confidence, bins_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(as_of, window_days) DO UPDATE SET
            walk_onset_grade_pct = excluded.walk_onset_grade_pct,
            descent_comfort_grade_pct = excluded.descent_comfort_grade_pct,
            confidence = excluded.confidence,
            bins_json = excluded.bins_json,
            created_at = excluded.created_at
        """,
        [
            as_of.isoformat(), window_days, model.walk_onset_grade_pct, model.descent_comfort_grade_pct,
            model.confidence, json.dumps(bins_payload), datetime.now(timezone.utc).isoformat(),
        ],
    )
    conn.commit()


def backfill_snapshots(conn: sqlite3.Connection, window_days: int, since: date | None = None) -> int:
    """Weekly snapshots from the earliest segmented activity to today; costly,
    since it walks every past week's window. since skips earlier weeks: a
    snapshot changes only when an activity dated on or before its as_of is
    added."""
    earliest = conn.execute("SELECT MIN(start_date) AS d FROM terrain_segment_dates WHERE segment_count > 0").fetchone()
    if earliest is None or earliest["d"] is None:
        return 0
    cursor = date.fromisoformat(str(earliest["d"])[:10])
    today = date.today()
    while since is not None and cursor < since:
        cursor += timedelta(days=7)
    count = 0
    while cursor <= today:
        record_snapshot(conn, as_of=cursor, window_days=window_days)
        count += 1
        cursor += timedelta(days=7)
    if count == 0 or cursor - timedelta(days=7) < today:
        record_snapshot(conn, as_of=today, window_days=window_days)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_terrain_profile(conn: sqlite3.Connection, window_days: int = ROLLING_WINDOW_DAYS) -> dict:
    """The model's grade bins, thresholds, and confidence over the window."""
    model = current_model(conn, window_days=window_days)
    return {
        "as_of": model.as_of,
        "window_days": model.window_days,
        "walk_onset_grade_pct": model.walk_onset_grade_pct,
        "descent_comfort_grade_pct": model.descent_comfort_grade_pct,
        "confidence": model.confidence,
        "bins": [b.to_dict() for b in model.bins],
    }


_SCALAR_TREND_METRICS = {"walk_onset_grade_pct", "descent_comfort_grade_pct"}
_BIN_TREND_METRICS = {"median_speed_mps", "efficiency_median", "walk_fraction"}


class TerrainTrendPoint(TypedDict):
    as_of: str
    value: float | None


def get_terrain_trend(
    conn: sqlite3.Connection,
    metric: str,
    grade_range: tuple[float, float] | None = None,
    since: str | None = None,
    window_days: int = ROLLING_WINDOW_DAYS,
) -> list[TerrainTrendPoint]:
    """One point per snapshot date, from snapshots taken with window_days. Bins averaged into a bin-based metric
    need MIN_SAMPLES_FOR_BIN samples."""
    if metric not in _SCALAR_TREND_METRICS and metric not in _BIN_TREND_METRICS:
        raise ValueError(f"Unknown terrain trend metric: {metric!r}")
    if metric in _BIN_TREND_METRICS and grade_range is None:
        raise ValueError(f"metric {metric!r} needs a grade_range")

    sql = (
        "SELECT as_of, walk_onset_grade_pct, descent_comfort_grade_pct, bins_json "
        "FROM terrain_snapshots WHERE window_days = ?"
    )
    params: list[object] = [window_days]
    if since:
        sql += " AND as_of >= ?"
        params.append(since)
    sql += " ORDER BY as_of"

    out: list[TerrainTrendPoint] = []
    for row in conn.execute(sql, params).fetchall():
        if metric in _SCALAR_TREND_METRICS:
            out.append({"as_of": row["as_of"], "value": row[metric]})
            continue
        lo, hi = cast(tuple[float, float], grade_range)
        bins = json.loads(row["bins_json"])
        selected = [
            b[metric]
            for b in bins
            if b["lo_pct"] is not None and b["hi_pct"] is not None
            and b["lo_pct"] >= lo and b["hi_pct"] <= hi and b[metric] is not None
            and b["sample_count"] >= MIN_SAMPLES_FOR_BIN
        ]
        value = sum(selected) / len(selected) if selected else None
        out.append({"as_of": row["as_of"], "value": value})
    return out


# ---------------------------------------------------------------------------
# Route scoring
# ---------------------------------------------------------------------------


class RouteGapBin(TypedDict):
    lo_pct: float | None
    hi_pct: float | None
    course_share: float
    deficit: float | None
    weighted_deficit: float | None


class RouteGapAnalysis(TypedDict):
    bins: list[RouteGapBin]


class RouteTerrainScore(TypedDict):
    route_id: int
    has_elevation: bool
    runnability: dict[str, float] | None
    gap_analysis: RouteGapAnalysis | None


def _segment_grades_pct(profile: Sequence[routes_service.RouteProfilePoint]) -> list[float | None]:
    """Grade of each segment between consecutive profile points; None where
    an endpoint has no elevation or the points coincide."""
    grades: list[float | None] = []
    for a, b in zip(profile, profile[1:]):
        seg = b["dist_m"] - a["dist_m"]
        if a["ele_m"] is None or b["ele_m"] is None or seg <= 0:
            grades.append(None)
        else:
            grades.append((b["ele_m"] - a["ele_m"]) / seg * 100.0)
    return grades


def _route_grades_pct(conn: sqlite3.Connection, route_id: int) -> list[float] | None:
    profile = routes_service.get_route_profile(conn, route_id)
    if len(profile) < 2 or any(p["ele_m"] is None for p in profile):
        return None
    return [g for g in _segment_grades_pct(profile) if g is not None]


def _gap_analysis(model: TerrainModel, grades_pct: Sequence[float]) -> RouteGapAnalysis:
    """Course grade histogram weighted by the athlete's per-bin efficiency
    deficit: how far a bin's grade-adjusted efficiency falls below their best
    bin."""
    total = len(grades_pct)
    histogram = Counter(grade_bin(g) for g in grades_pct)

    efficiencies = [b.efficiency_median for b in model.bins if b.efficiency_median is not None]
    best_efficiency = max(efficiencies) if efficiencies else None

    out: list[RouteGapBin] = []
    for index, count in sorted(histogram.items()):
        share = count / total
        bin_ = model.bins[index]
        deficit: float | None = None
        if best_efficiency and best_efficiency > 0 and bin_.efficiency_median is not None:
            deficit = max(0.0, (best_efficiency - bin_.efficiency_median) / best_efficiency)
        out.append({
            "lo_pct": _json_bound(bin_.lo_pct), "hi_pct": _json_bound(bin_.hi_pct), "course_share": share,
            "deficit": deficit,
            "weighted_deficit": (deficit * share) if deficit is not None else None,
        })
    return {"bins": out}


def score_route(conn: sqlite3.Connection, route_id: int) -> RouteTerrainScore:
    """Runnability breakdown of a stored route's grade profile under the
    current model. Raises routes.RouteNotFoundError for an unknown route_id."""
    grades = _route_grades_pct(conn, route_id)
    if grades is None:
        return {"route_id": route_id, "has_elevation": False, "runnability": None, "gap_analysis": None}
    model = current_model(conn)
    return {
        "route_id": route_id,
        "has_elevation": True,
        "runnability": model.score_profile(grades),
        "gap_analysis": _gap_analysis(model, grades),
    }


class RoutePointClass(TypedDict):
    idx: int
    lat: float
    lng: float
    dist_m: float
    ele_m: float | None
    grade_pct: float | None
    cls: Literal["run", "grind", "hike"] | None


def _classify_profile(
    conn: sqlite3.Connection, profile: Sequence[routes_service.RouteProfilePoint]
) -> list[RoutePointClass]:
    """Per-point runnability class for coloring. A point takes the grade of the
    segment to the next point; the last repeats the prior one to stay
    one-to-one with the profile."""
    if not profile:
        return []
    model = current_model(conn)
    grades = _segment_grades_pct(profile)
    grades.append(grades[-1] if grades else None)
    return [
        {
            "idx": p["idx"],
            "lat": p["lat"],
            "lng": p["lng"],
            "dist_m": p["dist_m"],
            "ele_m": p["ele_m"],
            "grade_pct": g,
            "cls": model.classify_grade(g) if g is not None else None,
        }
        for p, g in zip(profile, grades)
    ]


def route_point_classes(conn: sqlite3.Connection, route_id: int) -> list[RoutePointClass]:
    """Raises routes.RouteNotFoundError."""
    return _classify_profile(conn, routes_service.get_route_profile(conn, route_id))


def geometry_point_classes(
    conn: sqlite3.Connection, geometry: Sequence[Sequence[float | None]]
) -> list[RoutePointClass]:
    """route_point_classes for unsaved [lat, lng, ele_m|None] points, with the
    elevation smoothed as network.score_path does, so the classes agree with
    the candidate's own run/grind/hike shares. A path missing any elevation
    gets no classes."""
    points = [(cast(float, lat), cast(float, lng), ele) for lat, lng, ele in geometry]
    dist = gpx_parse.cumulative_distances_m(points)
    eles: list[float | None] = [p[2] for p in points]
    if len(points) > 1 and all(e is not None for e in eles):
        eles = list(gpx_parse.roughness_smoothed(dist, cast("list[float]", eles))[0])
    else:
        eles = [None] * len(points)
    return _classify_profile(conn, [
        {"idx": i, "lat": lat, "lng": lng, "dist_m": d, "ele_m": e}
        for i, ((lat, lng, _), d, e) in enumerate(zip(points, dist, eles))
    ])


# ---------------------------------------------------------------------------
# Activity analysis
# ---------------------------------------------------------------------------


class ClimbAnalysis(TypedDict):
    kind: str
    start_m: float
    end_m: float
    distance_m: float
    gain_m: float
    avg_grade_pct: float
    actual_speed_mps: float | None
    expected_speed_mps: float
    residual_mps: float | None
    percentile: float | None


class ActivityTerrainAnalysis(TypedDict):
    activity_id: int
    climbs: list[ClimbAnalysis]


def _percentile_at_grade(conn: sqlite3.Connection, grade_pct: float, speed_mps: float, window_days: int) -> float | None:
    lo, hi = _bin_bounds(grade_bin(grade_pct))
    start_date = (date.today() - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """
        SELECT ts.speed_cms / 100.0 AS speed_mps
        FROM terrain_segments ts
        JOIN terrain_segment_dates d ON d.activity_id = ts.activity_id
        WHERE ts.grade_dpct >= ? AND ts.grade_dpct < ? AND d.start_date >= ? AND ts.speed_cms IS NOT NULL
        """,
        [lo * 10, hi * 10, start_date],
    ).fetchall()
    speeds = [r["speed_mps"] for r in rows]
    if not speeds:
        return None
    below = sum(1 for s in speeds if s <= speed_mps)
    return 100.0 * below / len(speeds)


def analyze_activity_terrain(conn: sqlite3.Connection, activity_id: int) -> ActivityTerrainAnalysis:
    """Each climb/descent of the activity against the current model: residual
    from expected speed for that grade, fatigue, and temperature, and a
    percentile of the athlete's own history at that grade."""
    if not streams.has_cached_streams(activity_id):
        raise TerrainDataError(f"No current cached streams for activity {activity_id}")
    stream_df = streams.get_stream_dataframe(activity_id)
    resampled = _resample_altitude(stream_df)
    if resampled is None:
        raise TerrainDataError(f"Activity {activity_id} has no usable elevation stream")
    dist_out, ele_out = resampled["dist_m"], resampled["ele_m"]
    climbs = gpx_parse.segment_climbs(dist_out, ele_out)
    if not climbs:
        return {"activity_id": activity_id, "climbs": []}

    sub = cast(pd.DataFrame, stream_df.dropna(subset=["distance_m"]))
    raw_dist = [float(d) for d in sub["distance_m"].tolist()]
    elapsed_raw = _nearest(raw_dist, cast(pd.Series, sub["time_s"]), dist_out)
    elapsed_at = [_flt(v) for v in elapsed_raw]

    model = current_model(conn)
    temp_c = activity_temp_c(conn, activity_id, stream_df)

    results: list[ClimbAnalysis] = []
    for climb in climbs:
        start_idx = min(bisect.bisect_left(dist_out, climb["start_m"]), len(elapsed_at) - 1)
        end_idx = min(bisect.bisect_left(dist_out, climb["end_m"]), len(elapsed_at) - 1)
        t0, t1 = elapsed_at[start_idx], elapsed_at[end_idx]
        distance_m = climb["end_m"] - climb["start_m"]
        actual_speed = (distance_m / (t1 - t0)) if (t0 is not None and t1 is not None and t1 > t0) else None
        avg_grade_pct = climb["avg_grade"] * 100.0
        expected_speed = model.expected_speed_mps(avg_grade_pct, elapsed_s=t0 or 0.0, temp_c=temp_c)
        residual = (actual_speed - expected_speed) if actual_speed is not None else None
        percentile = (
            _percentile_at_grade(conn, avg_grade_pct, actual_speed, ROLLING_WINDOW_DAYS)
            if actual_speed is not None
            else None
        )
        results.append({
            "kind": climb["kind"],
            "start_m": climb["start_m"],
            "end_m": climb["end_m"],
            "distance_m": distance_m,
            "gain_m": climb["gain_m"],
            "avg_grade_pct": avg_grade_pct,
            "actual_speed_mps": actual_speed,
            "expected_speed_mps": expected_speed,
            "residual_mps": residual,
            "percentile": percentile,
        })
    return {"activity_id": activity_id, "climbs": results}
