"""
Detect race-anchored training builds within periods: hard-capped windows
trimmed back to where the volume ramp began. Periods describe continuity;
builds describe preparation for one race — this is deliberately capped so a
consistent year-round runner's period never reads as "the build."
Pure function of weekly aggregates, race references, and periods; no SQL, no I/O.
"""

from datetime import date, timedelta
from typing import TypedDict

from .periods import Period, WeekAgg, zero_fill

# Longest a build window can be, regardless of volume history.
MAX_BUILD_WEEKS = 18
# Builds shorter than this are still reported, flagged `thin` rather than dropped.
MIN_BUILD_WEEKS = 4
# Weeks after a prior anchor race before the next build window may start.
POST_RACE_RECOVERY_WEEKS = 2
# Only races at/above this distance anchor a build; shorter races still show in period race lists.
BUILD_ANCHOR_MIN_M = 9700.0
# A shorter tune-up race inside a build doesn't end the build; an equal-or-longer race does.
PRIOR_RACE_MIN_FRACTION = 0.9
# Ramp trim keeps weeks whose 3-week rolling volume is at least this fraction of the window peak.
RAMP_FLOOR_FRACTION = 0.5
# Sub-floor rolling weeks up to this run length stay inside the build; a longer dip splits it.
RAMP_DIP_TOLERANCE_WEEKS = 3


class RaceRef(TypedDict):
    date: str
    name: str | None
    distance_category: str
    distance_m: float


class BuildRace(TypedDict):
    date: str
    name: str | None
    distance_category: str


class Build(TypedDict):
    race: BuildRace
    start: str
    end: str
    weeks: int
    total_miles: float
    avg_mpw: float
    peak_3wk_avg: float
    workouts_per_week: float
    bounded_by: str
    thin: bool


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _rolling_3wk(miles: list[float]) -> list[float]:
    """Backward-looking 3-week average; the first two entries average what's available."""
    return [
        sum(miles[max(0, i - 2): i + 1]) / len(miles[max(0, i - 2): i + 1])
        for i in range(len(miles))
    ]


def detect_builds(weeks: list[WeekAgg], races: list[RaceRef], periods: list[Period]) -> list[Build]:
    filled = zero_fill(weeks)
    by_monday = {w["monday"]: w for w in filled}

    anchors = sorted(
        (r for r in races if r["distance_m"] >= BUILD_ANCHOR_MIN_M),
        key=lambda r: r["date"],
    )

    builds: list[Build] = []
    # (week Monday, distance_m) of every prior anchor race, in-period or not.
    prior_anchors: list[tuple[date, float]] = []

    for race in anchors:
        race_dt = date.fromisoformat(race["date"])
        race_monday = _monday(race_dt)

        period = next((p for p in periods if p["start"] <= race["date"] <= p["end"]), None)
        if period is None:
            # A race outside every period anchors nothing, but still counts as a prior anchor.
            prior_anchors.append((race_monday, race["distance_m"]))
            continue

        cap_bound = race_monday - timedelta(weeks=MAX_BUILD_WEEKS - 1)
        period_start_bound = date.fromisoformat(period["start"])
        # Latest prior anchor of comparable-or-longer distance; shorter tune-ups don't bound.
        bounding_prior = max(
            (m for m, dist in prior_anchors if dist >= PRIOR_RACE_MIN_FRACTION * race["distance_m"]),
            default=None,
        )
        # Order matters: ties resolve cap > prior_race > period_start (first match wins below).
        candidates: list[tuple[str, date]] = [("cap", cap_bound)]
        if bounding_prior is not None:
            candidates.append(("prior_race", bounding_prior + timedelta(weeks=POST_RACE_RECOVERY_WEEKS)))
        candidates.append(("period_start", period_start_bound))
        hard_start = max(b for _, b in candidates)
        bounded_by = next(name for name, b in candidates if b == hard_start)

        window_mondays: list[date] = []
        d = hard_start
        while d <= race_monday:
            window_mondays.append(d)
            d += timedelta(weeks=1)
        window_miles = [by_monday[m.isoformat()]["miles"] for m in window_mondays]

        rolling = _rolling_3wk(window_miles)
        peak_val = max(rolling)
        # Last occurrence on ties: the walk must pass over any dip between equal peaks.
        peak_idx = max(i for i, v in enumerate(rolling) if v == peak_val)

        # Walk backward from the peak; taper/peak weeks after the peak are never trimmed.
        # Sub-floor runs within the tolerance stay inside the build, but the build never
        # starts on a sub-floor week — leading tolerated-dip weeks are trimmed off the front.
        floor = RAMP_FLOOR_FRACTION * peak_val
        start_idx = peak_idx
        dip_run = 0
        for i in range(peak_idx - 1, -1, -1):
            if rolling[i] >= floor:
                dip_run = 0
                start_idx = i
            else:
                dip_run += 1
                if dip_run > RAMP_DIP_TOLERANCE_WEEKS:
                    break

        build_start = window_mondays[start_idx]
        if build_start > hard_start:
            bounded_by = "ramp"
        else:
            build_start = hard_start

        build_mondays = [m for m in window_mondays if m >= build_start]
        build_weeks_data = [by_monday[m.isoformat()] for m in build_mondays]
        weeks_n = len(build_mondays)
        total_miles = sum(w["miles"] for w in build_weeks_data)
        total_workouts = sum(w["workouts"] for w in build_weeks_data)

        builds.append({
            "race": {
                "date": race["date"],
                "name": race["name"],
                "distance_category": race["distance_category"],
            },
            "start": build_start.isoformat(),
            "end": race["date"],
            "weeks": weeks_n,
            "total_miles": round(total_miles, 1),
            "avg_mpw": round(total_miles / weeks_n, 1) if weeks_n else 0.0,
            "peak_3wk_avg": round(peak_val, 1),
            "workouts_per_week": round(total_workouts / weeks_n, 2) if weeks_n else 0.0,
            "bounded_by": bounded_by,
            "thin": weeks_n < MIN_BUILD_WEEKS,
        })

        prior_anchors.append((race_monday, race["distance_m"]))

    return builds
