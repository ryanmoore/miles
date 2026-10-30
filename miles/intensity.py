"""
Easy/moderate/hard time distribution per week, two ways side by side:
- hr: time by % of max HR, from the 20 m terrain segments where a run has
  them, else the whole run at its average HR.
- pace: each lap (or the whole run, without laps) by its heat-adjusted pace
  against the zones of the fitness checkpoint before the run's month, reusing
  fitness.intensity_for_pace (aerobic = easy, MP band = moderate, threshold
  and faster = hard).
Computed on request; nothing is stored.
"""

import sqlite3
from datetime import date, timedelta
from typing import Literal

from typing_extensions import TypedDict

from . import db
from .fitness import MILE_M, intensity_for_pace, zones_from_predicted
from .heat import activity_heat_factors
from .periods import monday_of
from .races import run_sport_filter

Zone = Literal["easy", "moderate", "hard"]

EASY_MAX_PCT_MAX_HR = 80
MODERATE_MAX_PCT_MAX_HR = 88

# Pace-zone labels from intensity_for_pace mapped onto three zones.
_PACE_LABEL_ZONE: dict[str, Zone] = {
    "aerobic": "easy",
    "MP": "moderate",
    "sub-MP": "moderate",
    "threshold": "hard",
    "sub-threshold": "hard",
    "interval": "hard",
    "sub-interval": "hard",
    "repetition": "hard",
    "sprint": "hard",
}

# Segment speeds below this (cm/s) are clamped so a standing pause can't
# dominate the time weighting.
_MIN_SEGMENT_SPEED_CMS = 30


class ZoneSeconds(TypedDict):
    easy: float
    moderate: float
    hard: float
    unclassified: float


class WeekIntensity(TypedDict):
    week_start: str
    hr: ZoneSeconds
    pace: ZoneSeconds


class IntensityResponse(TypedDict):
    max_hr: int | None
    hr_bounds_bpm: dict[str, int] | None  # easy_max / moderate_max
    weeks: list[WeekIntensity]
    totals: dict[str, ZoneSeconds]  # 'hr' / 'pace' over the whole window
    note: str | None


class RunRow(TypedDict):
    activity_id: int
    date: str
    moving_time_s: float
    distance_m: float
    average_heartrate: float | None  # None too when the athlete flagged the run's HR
    hr_trusted: bool  # False when flagged: its segment HR is not read either


def _zero() -> ZoneSeconds:
    return ZoneSeconds(easy=0.0, moderate=0.0, hard=0.0, unclassified=0.0)


def hr_zone(pct_max: float) -> Zone:
    if pct_max < EASY_MAX_PCT_MAX_HR:
        return "easy"
    if pct_max < MODERATE_MAX_PCT_MAX_HR:
        return "moderate"
    return "hard"


def runs_between(conn: sqlite3.Connection, start: date, end: date) -> list[RunRow]:
    """Runs with a positive moving time on start..end inclusive, oldest first."""
    clause, params = run_sport_filter()
    rows = conn.execute(f"""
        SELECT activity_id, DATE(start_date) AS d, moving_time_s, distance_m,
               {db.hr_sql("average_heartrate")} AS average_heartrate, {db.hr_trusted_sql()} AS hr_trusted
        FROM activities
        WHERE {clause} AND moving_time_s > 0
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        ORDER BY start_date
    """, params + [start.isoformat(), end.isoformat()]).fetchall()
    return [
        RunRow(
            activity_id=int(r["activity_id"]), date=r["d"], moving_time_s=float(r["moving_time_s"]),
            distance_m=float(r["distance_m"] or 0.0),
            average_heartrate=float(r["average_heartrate"]) if r["average_heartrate"] is not None else None,
            hr_trusted=bool(r["hr_trusted"]),
        )
        for r in rows
    ]


def hr_pct_seconds(conn: sqlite3.Connection, runs: list[RunRow], max_hr: int) -> dict[int, dict[int, float]]:
    """Per run, moving seconds at each whole % of max HR. Segment time is
    weighted by 1/speed (segments are equal length) and scaled to the run's
    moving time; runs without HR segments fall back to their average HR for
    the whole run. Runs with neither, or with their HR flagged, are absent."""
    out: dict[int, dict[int, float]] = {}
    by_id = {r["activity_id"]: r for r in runs}
    ids = [aid for aid, r in by_id.items() if r["hr_trusted"]]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        rows = conn.execute(f"""
            SELECT activity_id,
                   CAST(heartrate * 100.0 / ? AS INTEGER) AS pct,
                   SUM(1.0 / MAX(speed_cms, {_MIN_SEGMENT_SPEED_CMS})) AS w
            FROM terrain_segments
            WHERE activity_id IN ({','.join('?' * len(chunk))})
              AND heartrate IS NOT NULL AND speed_cms IS NOT NULL
            GROUP BY activity_id, pct
        """, [max_hr, *chunk]).fetchall()
        for r in rows:
            out.setdefault(int(r["activity_id"]), {})[int(r["pct"])] = float(r["w"])
    for aid, weights in out.items():
        total = sum(weights.values())
        scale = by_id[aid]["moving_time_s"] / total if total > 0 else 0.0
        for pct in weights:
            weights[pct] *= scale
    for run in runs:
        if run["activity_id"] not in out and run["average_heartrate"] is not None:
            out[run["activity_id"]] = {int(run["average_heartrate"] * 100.0 / max_hr): run["moving_time_s"]}
    return out


def _checkpoint_zones(conn: sqlite3.Connection) -> list[tuple[str, dict[str, float | str]]]:
    rows = conn.execute("""
        SELECT month, pace_5k, pace_marathon FROM fitness_checkpoints
        WHERE pace_5k IS NOT NULL AND pace_marathon IS NOT NULL
        ORDER BY month
    """).fetchall()
    return [(r["month"], zones_from_predicted(float(r["pace_marathon"]), float(r["pace_5k"]))) for r in rows]


def _zones_for(checkpoints: list[tuple[str, dict[str, float | str]]], run_date: str) -> dict[str, float | str] | None:
    """Zones from the latest checkpoint before the run's month (fitness as
    known going in), else the run's own month."""
    month = run_date[:7]
    prior = [z for m, z in checkpoints if m < month]
    if prior:
        return prior[-1]
    same = [z for m, z in checkpoints if m == month]
    return same[0] if same else None


def _pace_zone_seconds(conn: sqlite3.Connection, runs: list[RunRow]) -> dict[int, ZoneSeconds]:
    checkpoints = _checkpoint_zones(conn)
    factors = activity_heat_factors(conn, [r["activity_id"] for r in runs])
    laps_by_run: dict[int, list[tuple[float, float]]] = {}
    ids = [r["activity_id"] for r in runs]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(f"""
            SELECT activity_id, distance_m, moving_time_s FROM laps
            WHERE activity_id IN ({','.join('?' * len(chunk))})
              AND distance_m > 0 AND moving_time_s > 0
            ORDER BY activity_id, lap_index
        """, chunk).fetchall():
            laps_by_run.setdefault(int(r["activity_id"]), []).append((float(r["distance_m"]), float(r["moving_time_s"])))

    out: dict[int, ZoneSeconds] = {}
    for run in runs:
        zs = _zero()
        zones = _zones_for(checkpoints, run["date"])
        pieces = laps_by_run.get(run["activity_id"]) or [(run["distance_m"], run["moving_time_s"])]
        factor = factors.get(run["activity_id"], 1.0)
        for distance_m, time_s in pieces:
            if zones is None or distance_m <= 0:
                zs["unclassified"] += time_s
                continue
            pace = (time_s / 60.0) / (distance_m / MILE_M) / factor
            label = intensity_for_pace(pace, zones)
            zone = _PACE_LABEL_ZONE.get(label) if label is not None else None
            zs[zone if zone is not None else "unclassified"] += time_s
        out[run["activity_id"]] = zs
    return out


def _add(into: ZoneSeconds, other: ZoneSeconds) -> None:
    into["easy"] += other["easy"]
    into["moderate"] += other["moderate"]
    into["hard"] += other["hard"]
    into["unclassified"] += other["unclassified"]


def _rounded(z: ZoneSeconds) -> ZoneSeconds:
    return ZoneSeconds(
        easy=round(z["easy"]), moderate=round(z["moderate"]),
        hard=round(z["hard"]), unclassified=round(z["unclassified"]),
    )


def intensity_distribution(conn: sqlite3.Connection, weeks: int, today: date | None = None) -> IntensityResponse:
    """Weekly easy/moderate/hard seconds for the last `weeks` Mondays ending
    with the current week, by HR and by pace."""
    today = today or date.today()
    current_monday = monday_of(today)
    week_starts = [current_monday - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]
    runs = runs_between(conn, week_starts[0], current_monday + timedelta(days=6))

    athlete = db.get_athlete(conn)
    max_hr = athlete["max_hr"] if athlete else None
    note = None if max_hr else "No max HR configured; HR zones are unavailable (all time is unclassified)."

    hr_by_run: dict[int, ZoneSeconds] = {}
    if max_hr:
        for aid, pcts in hr_pct_seconds(conn, runs, max_hr).items():
            zs = _zero()
            for pct, secs in pcts.items():
                zs[hr_zone(pct)] += secs
            hr_by_run[aid] = zs
    pace_by_run = _pace_zone_seconds(conn, runs)

    by_week: dict[str, WeekIntensity] = {
        ws.isoformat(): WeekIntensity(week_start=ws.isoformat(), hr=_zero(), pace=_zero()) for ws in week_starts
    }
    for run in runs:
        week = by_week[monday_of(date.fromisoformat(run["date"])).isoformat()]
        hr = hr_by_run.get(run["activity_id"])
        if hr is None:
            hr = _zero()
            hr["unclassified"] = run["moving_time_s"]
        _add(week["hr"], hr)
        _add(week["pace"], pace_by_run[run["activity_id"]])

    totals = {"hr": _zero(), "pace": _zero()}
    for w in by_week.values():
        _add(totals["hr"], w["hr"])
        _add(totals["pace"], w["pace"])

    return IntensityResponse(
        max_hr=max_hr,
        hr_bounds_bpm={
            "easy_max": round(max_hr * EASY_MAX_PCT_MAX_HR / 100),
            "moderate_max": round(max_hr * MODERATE_MAX_PCT_MAX_HR / 100),
        } if max_hr else None,
        weeks=[
            WeekIntensity(week_start=w["week_start"], hr=_rounded(w["hr"]), pace=_rounded(w["pace"]))
            for w in by_week.values()
        ],
        totals={k: _rounded(v) for k, v in totals.items()},
        note=note,
    )
