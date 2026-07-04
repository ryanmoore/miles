"""Fitness chart drill-down for the Training page: all four projected paces per
checkpoint (distance selector) plus a recomputed evidence trail per checkpoint
month (click-a-point drill-down). See fitness.py for the estimate itself and
its tier-1/2/3 signal hierarchy — this module only re-presents that output;
it never invents its own fitness math.
"""

import re
import sqlite3
from datetime import date, timedelta
from typing import Literal, TypedDict, cast

from fastapi import APIRouter, HTTPException

from . import db
from .derive import ensure_derived
from .fitness import HALF_LIFE_DAYS, MILE_M, estimate_fitness
from .races import NOMINAL_METERS, classify_race_distance, riegel_time

router = APIRouter()

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# source_tier -> human basis; duplicated from api.py, which can't be imported
# here without a circular import.
_BASIS_BY_TIER: dict[int, str] = {1: "race", 2: "workout anchor", 3: "training floor"}
_KIND_BY_TIER: dict[int, Literal["race", "workout", "floor"]] = {1: "race", 2: "workout", 3: "floor"}


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    ensure_derived(conn)
    return conn


class FitnessTrendFullPoint(TypedDict):
    date: str
    confidence: str
    basis: str | None
    paces: dict[str, float]


@router.get("/api/fitness-trend-full")
def get_fitness_trend_full() -> list[FitnessTrendFullPoint]:
    """
    Monthly fitness checkpoints, oldest first, with all four predicted paces
    (5k/10k/half/marathon, decimal min/mi) per checkpoint — the distance-selector
    variant of /api/fitness-trend (api.py), which only carries pace_5k. Pure read
    of the derived fitness_checkpoints table; nothing is recomputed here.
    """
    conn = _conn()
    rows = conn.execute("""
        SELECT month, confidence, source_tier, pace_5k, pace_10k, pace_half, pace_marathon
        FROM fitness_checkpoints
        WHERE pace_5k IS NOT NULL
        ORDER BY month
    """).fetchall()

    return [
        FitnessTrendFullPoint(
            date=f"{r['month']}-01",
            confidence=cast(str, r["confidence"]),
            basis=_BASIS_BY_TIER.get(r["source_tier"]),
            paces={
                "5k": round(float(r["pace_5k"]), 2),
                "10k": round(float(r["pace_10k"]), 2),
                "half": round(float(r["pace_half"]), 2),
                "marathon": round(float(r["pace_marathon"]), 2),
            },
        )
        for r in rows
    ]


class Contributor(TypedDict):
    kind: Literal["race", "workout", "floor"]
    date: str
    name: str | None
    detail: str
    weight: float | None
    fivek_equiv_pace: float | None
    activity_id: int


class FitnessEvidence(TypedDict):
    month: str
    confidence: str
    fivek_pace: float
    contributors: list[Contributor]
    note: str | None


def _checkpoint_as_of(year: int, month: int) -> date:
    """Same per-month anchor derive.py's _fitness_checkpoints uses: the last day
    of the month, clamped to today for the current (still-in-progress) month."""
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return min(next_month - timedelta(days=1), date.today())


def _race_fivek_equiv(conn: sqlite3.Connection, activity_id: int) -> float | None:
    """Re-projects a tier-1 race source to a 5K-equivalent pace (decimal min/mi)
    via the same public Riegel projection fitness.py uses internally. Sources
    only carry activity_id/date/name/detail — not the per-source pace fitness.py
    computes and immediately folds into one recency-weighted average across all
    tier-1 races — so this is reconstructed from the raw activity row rather than
    threaded through estimate_fitness's return shape."""
    row = conn.execute(
        "SELECT distance_m, moving_time_s FROM activities WHERE activity_id = ?",
        [activity_id],
    ).fetchone()
    if row is None or row["distance_m"] is None or row["moving_time_s"] is None:
        return None
    category = classify_race_distance(row["distance_m"])
    if category is None:
        return None
    nominal_m = NOMINAL_METERS[category]
    t5k = riegel_time(float(row["moving_time_s"]), nominal_m, NOMINAL_METERS["5K"])
    return round((t5k / 60.0) / (NOMINAL_METERS["5K"] / MILE_M), 2)


@router.get("/api/fitness-evidence")
def get_fitness_evidence(month: str) -> FitnessEvidence:
    """
    Recomputes the fitness estimate as of the given checkpoint month's anchor
    date (matches derive.py's monthly-checkpoint convention exactly, including
    exclude_casual=True — the same final pass fitness_checkpoints stores) and
    returns its contributors: which race(s), workout anchor, or training-pace
    floor produced that month's number. Tier-1 (race) contributors carry a
    recency weight and a re-derived 5K-equivalent pace; tier-2/3 contributors
    are always their tier's sole source, so their 5K-equivalent pace is just the
    checkpoint's own predicted 5K (weight is not a concept for those tiers —
    returned as null rather than invented).

    422 on a malformed month; 404 when there's no fitness signal for it (before
    the first checkpoint, or a gap month with no trailing-window signal at all).
    """
    if not _MONTH_RE.match(month):
        raise HTTPException(422, f"Invalid month: {month!r} (expected YYYY-MM).")
    year, mon = int(month[:4]), int(month[5:7])

    conn = _conn()
    as_of = _checkpoint_as_of(year, mon)
    est = estimate_fitness(conn, as_of, exclude_casual=True)
    if est is None:
        raise HTTPException(404, f"No fitness checkpoint for {month}.")

    contributors: list[Contributor] = []
    for src in est["sources"]:
        tier = src["tier"]
        if tier == 1:
            age_days = (as_of - date.fromisoformat(src["date"])).days
            weight: float | None = round(0.5 ** (age_days / HALF_LIFE_DAYS), 2)
            fivek = _race_fivek_equiv(conn, src["activity_id"])
        else:
            # Sole source for its tier (see docstring) — the checkpoint's own
            # predicted 5K pace already is this contributor's 5K-equivalent.
            weight = None
            fivek = est["predicted"]["5K"]
        contributors.append(Contributor(
            kind=_KIND_BY_TIER[tier],
            date=src["date"],
            name=src["name"],
            detail=src["detail"],
            weight=weight,
            fivek_equiv_pace=fivek,
            activity_id=src["activity_id"],
        ))

    return FitnessEvidence(
        month=month,
        confidence=est["confidence"],
        fivek_pace=est["predicted"]["5K"],
        contributors=contributors,
        note=est.get("note"),
    )
