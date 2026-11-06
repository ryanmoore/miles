"""Strides and hill sprints found in a run's 20 m terrain segments, for runs
whose laps don't show them. Detections are proposals: nothing here writes notes."""

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple
from typing_extensions import TypedDict

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from . import terrain

M_PER_MILE = 1609.34
SEGMENT_M = 20.0
# Below this a segment is standing or GPS drift, not running; steep hiking is ~1 m/s.
MIN_MOVING_MPS = 0.6
# A burst is a short stretch clearly faster than the running just before and
# just after it (and than its own surroundings): strides on raw speed, hill
# sprints on speed relative to the model's speed for the grade. A sustained
# climb, progression or tempo has no slower side, or lasts too long.
CANDIDATE_RATIO = 1.15
BURST_RATIO = 1.3
GRADE_SMOOTH_SEGMENTS = 3
SURROUND_SEGMENTS = 15
SIDE_SEGMENTS = 10
MIN_SIDE_SEGMENTS = 5
STRIDE_S = (10.0, 40.0)
HILL_SPRINT_S = (8.0, 25.0)
HILL_GRADE_PCT = 4.0
MIN_BURSTS = 3


class Segment(NamedTuple):
    dist_m: float
    speed_mps: float
    grade_pct: float
    heartrate: float | None


class Element(TypedDict):
    type: str
    count: int
    duration_s: int
    where: str
    source: str


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Centered moving average; DEM grades jump segment to segment and would fake bursts."""
    padded = np.pad(values, window // 2, mode="edge")
    return np.convolve(padded, np.ones(window) / window, mode="valid")


def _bursts(metric: np.ndarray) -> list[tuple[int, int]]:
    """Index spans [i, j) of the metric that beat their surroundings and both neighbours."""
    padded = np.pad(metric, SURROUND_SEGMENTS, mode="edge")
    surround = np.median(sliding_window_view(padded, 2 * SURROUND_SEGMENTS + 1), axis=1)
    hot = metric >= CANDIDATE_RATIO * surround
    overall = np.median(metric)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(metric):
        if not hot[i]:
            i += 1
            continue
        j = i
        while j < len(metric) and hot[j]:
            j += 1
        sides = [metric[max(0, i - SIDE_SEGMENTS) : i], metric[j : j + SIDE_SEGMENTS]]
        refs = [np.percentile(w, 25) for w in sides if len(w) >= MIN_SIDE_SEGMENTS]
        level = np.median(metric[i:j])
        if refs and level >= BURST_RATIO * max(max(refs), overall):
            spans.append((i, j))
        i = j
    return spans


def detect_elements(
    segments: Sequence[Segment],
    expected_speed: Callable[[float], float],
    exclude_m: Sequence[tuple[float, float]] = (),
) -> list[Element]:
    """Strides and hill sprints: at least MIN_BURSTS bursts of the right length
    and grade, skipping any that overlap an `exclude_m` distance span (a
    workout's work laps)."""
    if len(segments) < 2 * SIDE_SEGMENTS:
        return []
    speed = np.array([s.speed_mps for s in segments])
    raw_grade = np.array([s.grade_pct for s in segments])
    grade = _smooth(raw_grade, GRADE_SMOOTH_SEGMENTS)
    effort = speed / np.array([expected_speed(g) for g in grade])

    bursts: list[tuple[float, float, float, str]] = []  # start_m, end_m, seconds, kind
    for metric, uphill, kind in ((speed, False, "strides"), (effort, True, "hill_sprints")):
        for i, j in _bursts(metric):
            start, end = segments[i].dist_m, segments[j - 1].dist_m + SEGMENT_M
            seconds = float(np.sum(SEGMENT_M / speed[i:j]))
            mean_grade = float(np.mean(raw_grade[i:j]))
            lo, hi = HILL_SPRINT_S if uphill else STRIDE_S
            # A hill sprint may be slow, but never slower than the run's own pace.
            if (mean_grade >= HILL_GRADE_PCT) == uphill and lo <= seconds <= hi and (
                not uphill or np.mean(speed[i:j]) >= np.median(speed)
            ) and not any(
                start < xhi and end > xlo for xlo, xhi in exclude_m
            ):
                bursts.append((start, end, seconds, kind))

    total_mi = (segments[-1].dist_m + SEGMENT_M) / M_PER_MILE
    found: list[Element] = []
    for kind in ("strides", "hill_sprints"):
        group = [b for b in bursts if b[3] == kind]
        if len(group) < MIN_BURSTS:
            continue
        first_mi, last_mi = group[0][0] / M_PER_MILE, group[-1][1] / M_PER_MILE
        where = f"last {total_mi - first_mi:.1f} mi" if first_mi >= 0.6 * total_mi else f"mi {first_mi:.1f}-{last_mi:.1f}"
        found.append({
            "type": kind,
            "count": len(group),
            "duration_s": round(statistics.median(b[2] for b in group)),
            "where": where,
            "source": "stream",
        })
    return found


def load_segments(conn: sqlite3.Connection, activity_ids: Sequence[int]) -> dict[int, list[Segment]]:
    """Moving segments with a speed, per activity, in order; absent for runs with no stored stream."""
    out: dict[int, list[Segment]] = defaultdict(list)
    rows = conn.execute(
        f"""
        SELECT activity_id, dist_m, speed_cms, grade_dpct, heartrate FROM terrain_segments
        WHERE activity_id IN ({','.join('?' * len(activity_ids))}) AND speed_cms >= ? ORDER BY activity_id, idx
        """,
        [*activity_ids, MIN_MOVING_MPS * 100],
    )
    for r in rows:
        out[r["activity_id"]].append(Segment(r["dist_m"], r["speed_cms"] / 100.0, r["grade_dpct"] / 10.0, r["heartrate"]))
    return out


def work_lap_spans(conn: sqlite3.Connection, activity_ids: Sequence[int]) -> dict[int, list[tuple[float, float]]]:
    """Distance spans (m from the start) of each activity's work laps."""
    spans: dict[int, list[tuple[float, float]]] = defaultdict(list)
    covered: dict[int, float] = defaultdict(float)
    rows = conn.execute(
        f"SELECT activity_id, distance_m, lap_type FROM laps WHERE activity_id IN ({','.join('?' * len(activity_ids))}) "
        "ORDER BY activity_id, lap_index",
        list(activity_ids),
    )
    for r in rows:
        start = covered[r["activity_id"]]
        covered[r["activity_id"]] = start + (r["distance_m"] or 0.0)
        if r["lap_type"] == "work":
            spans[r["activity_id"]].append((start, covered[r["activity_id"]]))
    return spans


def detect_for_activities(conn: sqlite3.Connection, activity_ids: Sequence[int]) -> dict[int, list[Element]]:
    """Detections per activity that has stored segments (possibly none), against generic grade-speed expectations."""
    if not activity_ids:
        return {}
    expected = terrain.default_model().expected_speed_mps
    segments, spans = load_segments(conn, activity_ids), work_lap_spans(conn, activity_ids)
    return {a: detect_elements(segs, expected, spans.get(a, ())) for a, segs in segments.items()}


def unlisted(noted: object, detected: Sequence[Element]) -> list[Element]:
    """Detections of types the run's note (its `elements` value, if any) doesn't
    list. A type the note lists is the athlete's word, whatever its count:
    `count: 0` rejects a false detection."""
    listed = {e["type"] for e in noted} if isinstance(noted, list) else set()
    return [d for d in detected if d["type"] not in listed]


def merged(noted: object, detected: Sequence[Element]) -> list[Mapping[str, Any]]:
    """A run's elements: the note's entries plus the detections it doesn't list."""
    return [*(noted if isinstance(noted, list) else ()), *unlisted(noted, detected)]
