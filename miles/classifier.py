import re
from typing import Literal

# Coarse keyword patterns — match workout *type*, not rep count or duration.
# "3x3min LT" and "6x3min LT" both → "LT"; the per-rep structure lives in laps.
WORKOUT_LABEL_PATTERNS: list[tuple[str, str]] = [
    (r"\bflux\b", "MP Flux"),
    (r"\blt\b|sublt|\bsub.lt\b", "LT"),  # "@ LT", "subLT", "sub-LT"
    (r"\d+x\d+min\b", "LT"),              # Nx3min, Nx6min interval format
    (r"\d+x\d+s\b", "LT"),               # Nx70s interval format
    (r"\btempo\b", "Tempo"),
    (r"\bstride", "Strides"),
    (r"\bfartlek\b", "Fartlek"),
    (r"\bprogression\b", "Progression"),
    (r"\bhill", "Hills"),
]


def classify_workout(name: str) -> str | None:
    for pattern, label in WORKOUT_LABEL_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            return label
    return None


LapType = Literal["warmup", "work", "recovery", "float", "cooldown", "steady"]

# Below this per-lap floor, pace/HR are too noisy to run the work-block algorithm
# on directly (GPS blips, button-mash laps). classify_laps still recovers a type
# for these when their neighbors show they're genuine between-rep rests.
LAP_MIN_DISTANCE_M = 200
LAP_MIN_MOVING_TIME_S = 45

# Types that mark a lap as belonging to a detected work block (as opposed to
# warmup/cooldown outside it, or "steady" when no block was detected at all).
_BLOCK_TYPES = {"work", "recovery", "float"}


def _is_classifiable(distance_m: float, moving_time_s: float, speed_mps: float) -> bool:
    return (
        distance_m >= LAP_MIN_DISTANCE_M
        and moving_time_s >= LAP_MIN_MOVING_TIME_S
        and speed_mps > 0
    )


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2]


def _gap_split(
    idx: list[int],
    speeds: list[float],
    distances_m: list[float],
    min_gap: float = 0.08,
) -> set[int] | None:
    """
    Split laps (by index) at the largest relative speed gap; None if no meaningful gap.
    The fast side must be at least 2 laps or 25% of the distance — otherwise a single
    outlier lap (e.g. a closing stride) would swallow the whole session as "slow",
    so such gaps are skipped in favor of the next largest.
    """
    order = sorted(idx, key=lambda i: speeds[i])
    total_dist = sum(distances_m[i] for i in idx)
    gaps: list[tuple[float, int]] = []
    for k in range(len(order) - 1):
        lo, hi = speeds[order[k]], speeds[order[k + 1]]
        gaps.append(((hi - lo) / hi, k))
    for gap, k in sorted(gaps, reverse=True):
        if gap < min_gap:
            return None
        fast = order[k + 1:]
        if len(fast) >= 2 or sum(distances_m[i] for i in fast) >= 0.25 * total_dist:
            return set(order[: k + 1])  # the slow side
    return None


def _edge_gap_split(speeds: list[float], distances_m: list[float]) -> set[int] | None:
    """
    Session-level split (min gap 6%). A split whose slow laps are all interior only
    sets jog-recovery outliers apart, leaving warmup/cooldown counted as work: set
    those aside and retry. A retried edge split needs a fast side of at least a
    quarter of the remaining distance; otherwise the first split stands.
    """
    cand = list(range(len(speeds)))
    first: set[int] | None = None
    interior: set[int] = set()
    for _ in range(len(speeds)):
        slow = _gap_split(cand, speeds, distances_m, min_gap=0.06)
        if slow is None:
            break
        first = first or slow
        if cand[0] in slow or cand[-1] in slow:
            fast_m = sum(distances_m[i] for i in cand if i not in slow)
            if not interior or fast_m >= 0.25 * sum(distances_m[i] for i in cand):
                return slow | interior
            break
        interior |= slow
        cand = [i for i in cand if i not in slow]
    return first


def _classify_block(
    speeds: list[float],
    distances_m: list[float],
    heartrates: list[float | None],
) -> list[LapType]:
    """
    Classify each floor-passing lap of a session as warmup / work / recovery /
    float / cooldown.

    Positional "work block" approach: warmup and cooldown are defined by where they
    sit relative to the fast laps, not by pace — so warmup miles are separated from
    interval recoveries even when their paces overlap.

    1. Split fast/slow at the largest relative speed gap that reaches an edge lap
       ("steady" if no gap >= 6%).
    2. The work block spans first to last fast lap; laps before/after are
       warmup/cooldown, slow laps inside are recovery.
    3. A second gap split inside the block separates jog recoveries from reps when
       the session-level gap landed at the cooldown instead.
    4. Block-edge laps with HR far below the work median, pace materially slower,
       and a non-modal distance are trimmed to warmup/cooldown (catches warmup
       miles that pace alone can't separate, e.g. before MP flux).
    5. In-block recovery laps at near-work HR become "float" (continuous work,
       e.g. flux slow halves), distinct from true jog recoveries.

    Known limitation: uphill reps invert the pace signal (slow pace, high HR) and
    classify as float rather than work.
    """
    n = len(speeds)
    if n == 0:
        return []

    # Session level uses a lower gap floor: warmup-to-work contrast can be modest
    # (e.g. flux with a brisk warmup), while the 8% inner floor keeps intentional
    # alternation (flux fast/slow halves, ~7% apart) intact as one work block.
    slow_set = _edge_gap_split(speeds, distances_m)
    if slow_set is None:
        return ["steady"] * n

    fast_idx = [i for i in range(n) if i not in slow_set]
    first, last = fast_idx[0], fast_idx[-1]
    types: list[LapType] = [
        "warmup" if i < first else "cooldown" if i > last
        else ("recovery" if i in slow_set else "work")
        for i in range(n)
    ]

    block_idx = list(range(first, last + 1))
    if len(block_idx) >= 3:
        inner_slow = _gap_split(block_idx, speeds, distances_m)
        if inner_slow is not None:
            for i in block_idx:
                if i in inner_slow and types[i] == "work":
                    types[i] = "recovery"

    work_i = [i for i in block_idx if types[i] == "work"]
    work_hrs = [h for i in work_i if (h := heartrates[i]) is not None]
    if work_i and work_hrs and len(block_idx) >= 3:
        hr_cut = _median(work_hrs) - 15
        speed_cut = _median([speeds[i] for i in work_i]) * 0.95
        med_dist = _median([distances_m[i] for i in block_idx])

        def is_edge_noise(i: int) -> bool:
            h = heartrates[i]
            return (h is not None and h < hr_cut
                    and speeds[i] < speed_cut
                    and abs(distances_m[i] - med_dist) / med_dist > 0.3)

        i = first
        while i <= last and is_edge_noise(i):
            types[i] = "warmup"
            i += 1
        i = last
        while i >= first and is_edge_noise(i):
            types[i] = "cooldown"
            i -= 1

    rec_hrs = [h for i in range(n) if types[i] == "recovery" and (h := heartrates[i]) is not None]
    if work_hrs and rec_hrs and _median(rec_hrs) > _median(work_hrs) - 12:
        types = ["float" if t == "recovery" else t for t in types]
    return types


def classify_laps(
    speeds: list[float],
    distances_m: list[float],
    heartrates: list[float | None],
    moving_times_s: list[float],
) -> list[LapType | None]:
    """
    Classify every lap of a session, including those below the classification
    floor (LAP_MIN_DISTANCE_M / LAP_MIN_MOVING_TIME_S).

    Floor-passing laps run the full work-block algorithm (_classify_block). A
    sub-floor lap is never fed into that algorithm directly — its pace/HR are too
    noisy — but if it sits between two floor-passing laps that both landed inside
    the work block (work/recovery/float), it's a genuine between-rep rest and gets
    "recovery" too. Sub-floor laps outside any block (warmup fiddling, trailing
    artifacts, or sessions with no block at all) stay unclassified (None).
    """
    n = len(speeds)
    if n == 0:
        return []

    classifiable = [
        i for i in range(n)
        if _is_classifiable(distances_m[i], moving_times_s[i], speeds[i])
    ]
    if not classifiable:
        return [None] * n

    block_types = _classify_block(
        [speeds[i] for i in classifiable],
        [distances_m[i] for i in classifiable],
        [heartrates[i] for i in classifiable],
    )

    types: list[LapType | None] = [None] * n
    for i, t in zip(classifiable, block_types):
        types[i] = t

    classifiable_set = set(classifiable)
    base_types = list(types)  # snapshot: neighbor lookups ignore laps backfilled below
    for i in range(n):
        if i in classifiable_set:
            continue
        prev_t = next((base_types[j] for j in range(i - 1, -1, -1) if base_types[j] is not None), None)
        next_t = next((base_types[j] for j in range(i + 1, n) if base_types[j] is not None), None)
        if prev_t in _BLOCK_TYPES and next_t in _BLOCK_TYPES:
            types[i] = "recovery"

    return types
