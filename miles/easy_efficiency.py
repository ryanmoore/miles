"""Easy-run efficiency factor (EF): speed per heartbeat on runs whose
effective type is easy, the training page's "Easy-run efficiency" chart.

Computed from terrain segments, not the activity's averages, so each run
can drop its warmup and walking, skip pauses, and grade-adjust its distance:
every 20 m segment counts as 20 m x its Minetti cost ratio, with the grade
smoothed over a few segments first (the cost curve is nonlinear, so raw
segment-to-segment grade noise would bias it upward). EF is that distance
over the run's time, per beat of its time-weighted mean HR. The heat factor
is returned beside it rather than applied, so the client can toggle it.
Computed on request, nothing stored.
"""

import sqlite3
from typing import cast

import pandas as pd
from fastapi import APIRouter
from typing_extensions import TypedDict

from . import db, heat, streams
from .derive import connect_derived
from .races import run_sport_filter

router = APIRouter()

WARMUP_S = 600  # HR is still catching up with the pace before this
MIN_STEADY_S = 20 * 60  # a run needs this much time past the warmup to count
MAX_SEGMENT_GAP_S = 20  # a longer step between segment starts is a pause
GRADE_SMOOTH_SEGMENTS = 5  # centred rolling mean, ~100 m
SEGMENT_M = 20


class EasyRunEfficiency(TypedDict):
    activity_id: int
    name: str | None
    start_date: str
    minutes: float  # counted time (past the warmup, no pauses or walking)
    avg_hr: float  # time-weighted over the counted time
    pace_s: float  # seconds/mile over the counted time
    gap_pace_s: float  # same, grade-adjusted
    heat_factor: float  # 1.0 without weather; multiply EF by it to heat-adjust
    ef: float  # meters per beat, raw
    ef_gap: float  # meters per beat, grade-adjusted


_COLUMNS = ["activity_id", "dist_m", "elapsed_s", "grade_dpct", "heartrate", "is_walking"]


def easy_efficiency(conn: sqlite3.Connection) -> list[EasyRunEfficiency]:
    tc, tp = run_sport_filter("a")
    cursor = conn.execute(f"""
        SELECT s.activity_id, s.dist_m, s.elapsed_s, s.grade_dpct, s.heartrate, s.is_walking
        FROM terrain_segments s
        JOIN activities a ON a.activity_id = s.activity_id
        WHERE {tc} AND {db.effective_run_type_sql("a")} = 'easy' AND {db.hr_trusted_sql("a")}
        ORDER BY s.activity_id, s.idx
    """, tp)
    cursor.row_factory = None  # plain tuples: building a dict per row dominates the fetch
    rows = cursor.fetchall()
    if not rows:
        return []

    df = pd.DataFrame.from_records(rows, columns=_COLUMNS)
    by_run = df.groupby("activity_id", sort=False)
    grade = cast(pd.Series, by_run["grade_dpct"].transform(
        lambda g: g.rolling(GRADE_SMOOTH_SEGMENTS, center=True, min_periods=1).mean()
    )) / 10.0
    # Each segment runs until the next one starts.
    df["dt"] = by_run["elapsed_s"].shift(-1) - df["elapsed_s"]
    df["ddist"] = by_run["dist_m"].shift(-1) - df["dist_m"]
    df["cost"] = streams.minetti_cost_ratios(cast(pd.Series, grade))
    keep = (
        (df["elapsed_s"] >= WARMUP_S)
        & (df["is_walking"] == 0)
        & df["heartrate"].notna() & (df["heartrate"] > 0)
        & (df["dt"] > 0) & (df["dt"] <= MAX_SEGMENT_GAP_S)
        # Paused stretches' segments are dropped, leaving a distance jump.
        & ((df["ddist"] - SEGMENT_M).abs() <= 1)
    )
    kept = cast(pd.DataFrame, df[keep]).copy()
    kept["gdist"] = kept["ddist"] * kept["cost"]
    kept["hr_dt"] = kept["heartrate"] * kept["dt"]
    sums = kept.groupby("activity_id").agg(
        t=("dt", "sum"), dist=("ddist", "sum"), gdist=("gdist", "sum"), hr_dt=("hr_dt", "sum"),
    )
    sums = cast(pd.DataFrame, sums[sums["t"] >= MIN_STEADY_S])
    if sums.empty:
        return []

    ids = [int(i) for i in sums.index]
    meta = {
        int(r["activity_id"]): r
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500))
        for r in conn.execute(
            f"SELECT activity_id, name, start_date FROM activities WHERE activity_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
    }
    factors = heat.activity_heat_factors(conn, ids)

    out: list[EasyRunEfficiency] = []
    columns = (sums.index.tolist(), sums["t"].tolist(), sums["dist"].tolist(), sums["gdist"].tolist(), sums["hr_dt"].tolist())
    for activity_id, t, dist, gdist, hr_dt in zip(*columns):
        aid = int(activity_id)
        avg_hr = hr_dt / t
        beats = avg_hr * t / 60.0
        m = meta[aid]
        out.append(EasyRunEfficiency(
            activity_id=aid,
            name=m["name"],
            start_date=m["start_date"],
            minutes=round(t / 60.0, 1),
            avg_hr=round(avg_hr, 1),
            pace_s=round(t / dist * 1609.34, 1),
            gap_pace_s=round(t / gdist * 1609.34, 1),
            heat_factor=round(factors.get(aid, 1.0), 4),
            ef=round(dist / beats, 4),
            ef_gap=round(gdist / beats, 4),
        ))
    out.sort(key=lambda e: e["start_date"])
    return out


@router.get("/api/easy-efficiency")
def get_easy_efficiency() -> list[EasyRunEfficiency]:
    """One row per easy run with enough steady time: speed per heartbeat,
    raw and grade-adjusted, plus its heat factor."""
    with db.connection(connect_derived) as conn:
        return easy_efficiency(conn)
