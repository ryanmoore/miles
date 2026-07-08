"""
Detect training periods: stretches of consistent running separated by gaps.

Periods describe *continuity*, not race preparation — deliberately no maximum
length, so a consistent year-round runner correctly yields one long period.
Pure function of weekly aggregates; no SQL, no I/O, fully deterministic.
"""

from datetime import date, timedelta
from typing_extensions import TypedDict

# A week counts as active if it clears either threshold.
ACTIVE_WEEK_MIN_RUNS = 2
ACTIVE_WEEK_MIN_MILES = 8.0

# Consecutive inactive weeks at/above this count ends a period; below it, the
# gap stays interior (counted in weeks_total, not weeks_active).
GAP_WEEKS_TO_SPLIT = 3

# Active-week clusters shorter than this are reported, not dropped, but flagged.
MIN_PERIOD_ACTIVE_WEEKS = 3


class WeekAgg(TypedDict):
    monday: str
    miles: float
    runs: int
    workouts: int


class Period(TypedDict):
    start: str
    end: str
    weeks_total: int
    weeks_active: int
    total_miles: float
    avg_mpw_active: float
    peak_week_miles: float
    runs: int
    fragment: bool


class Gap(TypedDict):
    start: str
    end: str
    weeks: int


def is_active(week: WeekAgg) -> bool:
    return week["runs"] >= ACTIVE_WEEK_MIN_RUNS or week["miles"] >= ACTIVE_WEEK_MIN_MILES


def sunday_of(monday_iso: str) -> str:
    return (date.fromisoformat(monday_iso) + timedelta(days=6)).isoformat()


def zero_fill(weeks: list[WeekAgg]) -> list[WeekAgg]:
    """Fill every calendar week between the first and last given Monday with a zero week."""
    if not weeks:
        return []
    given = {w["monday"]: w for w in weeks}
    start = date.fromisoformat(min(given))
    end = date.fromisoformat(max(given))

    filled: list[WeekAgg] = []
    d = start
    while d <= end:
        iso = d.isoformat()
        filled.append(given.get(iso, {"monday": iso, "miles": 0.0, "runs": 0, "workouts": 0}))
        d += timedelta(weeks=1)
    return filled


def detect_periods(weeks: list[WeekAgg]) -> tuple[list[Period], list[Gap]]:
    """
    Segment zero-filled weeks into periods (active-week clusters) and the gaps
    between them. Leading/trailing inactive weeks belong to no period and are
    not reported as gaps (only inter-period stretches are).
    """
    filled = zero_fill(weeks)
    active_idxs = [i for i, w in enumerate(filled) if is_active(w)]
    if not active_idxs:
        return [], []

    clusters: list[list[int]] = [[active_idxs[0]]]
    for idx in active_idxs[1:]:
        if idx - clusters[-1][-1] - 1 >= GAP_WEEKS_TO_SPLIT:
            clusters.append([idx])
        else:
            clusters[-1].append(idx)

    periods: list[Period] = []
    for cluster in clusters:
        first_i, last_i = cluster[0], cluster[-1]
        span = filled[first_i : last_i + 1]
        total_miles = sum(w["miles"] for w in span)
        weeks_active = len(cluster)
        periods.append({
            "start": filled[first_i]["monday"],
            "end": sunday_of(filled[last_i]["monday"]),
            "weeks_total": len(span),
            "weeks_active": weeks_active,
            "total_miles": round(total_miles, 1),
            "avg_mpw_active": round(total_miles / weeks_active, 1),
            "peak_week_miles": round(max(w["miles"] for w in span), 1),
            "runs": sum(w["runs"] for w in span),
            "fragment": weeks_active < MIN_PERIOD_ACTIVE_WEEKS,
        })

    gaps: list[Gap] = []
    for prev_cluster, next_cluster in zip(clusters, clusters[1:]):
        gap_start_i, gap_end_i = prev_cluster[-1] + 1, next_cluster[0] - 1
        gaps.append({
            "start": filled[gap_start_i]["monday"],
            "end": sunday_of(filled[gap_end_i]["monday"]),
            "weeks": gap_end_i - gap_start_i + 1,
        })

    return periods, gaps
