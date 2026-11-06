"""
Plan schema accessors: pure helpers over the plans/plan_versions/plan_weeks/
plan_days/plan_log tables (miles/db.py). Plans are athlete-authored ground
truth, like activities — not derived, exempt from derive_all.

Validation lives here, never in callers: week_starts must be Mondays,
contiguous, covering the race week when a race is set; targets non-negative;
committed plans never share a week; versions are immutable append-only
snapshots (there is no UPDATE path).
Zone-anchored day targets freeze to concrete pace ranges at authoring time via
a live estimate_fitness — see _freeze_day_target.

All errors raise PlanValidationError with messages naming the offending
week/day/field, so the MCP layer can surface them to a planner agent verbatim.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Literal, NotRequired, cast

from typing_extensions import TypedDict

from .db import PlanDayRow, PlanGoalRow, PlanRow, PlanVersionRow, PlanWeekRow, effective_run_type_sql
from .fitness import (
    INTERVAL_TOLERANCE,
    MP_TOLERANCE,
    REPETITION_TOLERANCE,
    THRESHOLD_TOLERANCE,
    estimate_fitness,
    zones_from_predicted,
)
from .periods import monday_of
from .races import classify_race_distance

# Closed vocabularies — validation rejects anything outside them.
PHASES: tuple[str, ...] = ("base", "sharpen", "peak", "taper", "race")
SLOTS: tuple[str, ...] = ("easy", "workout", "long", "rest", "race", "strength")
LOG_ACTIONS: tuple[str, ...] = ("skipped", "moved", "modified", "note")
# terrain composes with any run slot rather than being a slot itself (a trail
# long run is still a long run); NULL in plan_days.terrain means road.
TERRAIN_VALUES: tuple[str, ...] = ("road", "trail")
# zone_name vocabulary is exactly the zones_from_predicted anchors, plus "easy"
# (which freezes to the easy_range band rather than a zones_from_predicted key).
ZONE_NAMES: tuple[str, ...] = ("easy", "marathon", "threshold", "interval", "repetition")

# easy freezes to [MP + EASY_LO_OFFSET, MP + EASY_HI_OFFSET] — matches the
# easy_range band fitness.zones_from_predicted computes (MP+1:00 to MP+1:45/mi).
EASY_LO_OFFSET_MIN = 1.0
EASY_HI_OFFSET_MIN = 1.75

_ZONE_TOLERANCE: dict[str, float] = {
    "marathon": MP_TOLERANCE,
    "threshold": THRESHOLD_TOLERANCE,
    "interval": INTERVAL_TOLERANCE,
    "repetition": REPETITION_TOLERANCE,
}

_RACE_FIELDS: tuple[str, ...] = ("race_date", "distance_bucket", "goal_time_s")
_WEEK_DIFF_FIELDS: tuple[str, ...] = (
    "target_miles", "target_workouts", "target_long_run_miles", "phase", "note",
)
_DAY_DIFF_FIELDS: tuple[str, ...] = ("slot", "title", "target_miles", "target_json")

# Window (inclusive, either side) around a plan's race_date within which a
# synced effective-race activity can auto-complete the plan — covers
# timezone edges where Strava's local start_date lands a day off race_date.
RACE_MATCH_WINDOW_DAYS = 1

# Run slots contribute mileage; rest/strength never do.
MILEAGE_SLOTS: frozenset[str] = frozenset({"easy", "workout", "long", "race"})
# Tolerance (miles) absorbing per-day rounding when checking a week's authored
# mileage band against the sum of its days — a real drift is far larger.
WEEK_MILEAGE_TOLERANCE_MI = 0.5


class PlanValidationError(Exception):
    """Raised for any plan/version/log validation failure. Messages name the
    offending week/day/field so they can be relayed to a planner agent as-is."""


class DayTarget(TypedDict, total=False):
    """Contents of plan_days.target_json (JSON-encoded). All fields optional.
    reps/rep_distance_m sketch a workout's structure; pace_lo/pace_hi are
    decimal min/mi, frozen at authoring (either given explicitly or resolved
    from zone_name via a live fitness estimate — see _freeze_day_target).
    zone_name is kept alongside the frozen paces for display. reps_lo/reps_hi
    express a rep range ("8-10 x..."); reps stays the point form. Time-based
    reps use rep_duration_s alongside/instead of rep_distance_m."""
    reps: int
    reps_lo: int
    reps_hi: int
    rep_duration_s: float
    rep_distance_m: float
    pace_lo: float
    pace_hi: float
    zone_name: str
    hr_lo: float
    hr_hi: float


class WeekInput(TypedDict):
    """upsert_draft_weeks input for one plan_weeks row.
    target_miles is the range floor (point week = lo only, no hi); omitting
    both target_miles and target_miles_hi is a deliberately unspecified week,
    scored on workout count alone. target_miles_hi/target_long_run_minutes
    are optional range/duration targets alongside the mileage ones."""
    week_start: str
    target_workouts: int
    phase: str
    target_miles: NotRequired[float | None]
    target_miles_hi: NotRequired[float | None]
    target_long_run_miles: NotRequired[float | None]
    target_long_run_minutes: NotRequired[float | None]
    note: NotRequired[str | None]


class DayInput(TypedDict):
    """upsert_draft_days input for one plan_days row. target is
    resolved/frozen before storage in a committed version; see
    _freeze_day_target (drafts store it validated but unresolved — see
    _validate_day_target)."""
    date: str
    slot: str
    seq: NotRequired[int]
    title: NotRequired[str | None]
    target_miles: NotRequired[float | None]
    target: NotRequired[DayTarget | None]
    terrain: NotRequired[str | None]
    note: NotRequired[str | None]
    target_minutes: NotRequired[float | None]


class PlanVersionBundle(TypedDict):
    """A full version snapshot: the version row plus every week/day/goal row."""
    version: PlanVersionRow
    weeks: list[PlanWeekRow]
    days: list[PlanDayRow]
    goals: list[PlanGoalRow]


class DraftBundle(TypedDict):
    """Current state of a plan's one mutable draft, plus a gap report: plain-
    English messages naming what commit_plan's global validation would
    reject right now (unauthored weeks, days with no week row, not yet
    reaching the race week) — see get_draft."""
    plan: PlanRow
    version: PlanVersionRow
    weeks: list[PlanWeekRow]
    days: list[PlanDayRow]
    goals: list[PlanGoalRow]
    gaps: list[str]


class WeekMileageCheck(TypedDict):
    """Program-computed day-mileage sum for one week vs. its authored band —
    see compute_week_mileage."""
    week_start: str
    day_miles_total: float
    days_missing_miles: int
    target_miles: float | None
    target_miles_hi: float | None
    consistent: bool
    message: str | None


class WeekDiff(TypedDict):
    week_start: str
    change: Literal["added", "removed", "changed"]
    changed_fields: list[str]  # populated only when change == "changed"


class DayDiff(TypedDict):
    date: str
    seq: int
    change: Literal["added", "removed", "changed"]
    changed_fields: list[str]  # populated only when change == "changed"


class GoalDiff(TypedDict):
    key: str
    change: Literal["added", "removed", "changed"]


class VersionDiff(TypedDict):
    version_a: int
    version_b: int
    changed_race: dict[str, tuple[object, object]]  # race field -> (value in a, value in b)
    changed_weeks: list[WeekDiff]
    changed_days: list[DayDiff]
    changed_goals: list[GoalDiff]


class RaceMatch(TypedDict):
    """A synced effective-race activity matching a plan's race_date (+/-
    RACE_MATCH_WINDOW_DAYS) and distance_bucket (case-insensitive against
    classify_race_distance's vocabulary) — see find_completing_race."""
    activity_id: int
    name: str | None
    date: str
    distance_m: float | None
    moving_time_s: int | None


# --- row builders -----------------------------------------------------------

def _plan_row(row: sqlite3.Row) -> PlanRow:
    return {
        "plan_id": int(row["plan_id"]),
        "title": row["title"],
        "race_date": row["race_date"],
        "distance_bucket": row["distance_bucket"],
        "goal_time_s": int(row["goal_time_s"]) if row["goal_time_s"] is not None else None,
        "status": row["status"],
        "created_at": row["created_at"],
        "first_week": row["first_week"],
        "last_week": row["last_week"],
    }


def _version_row(row: sqlite3.Row) -> PlanVersionRow:
    return {
        "version_id": int(row["version_id"]),
        "plan_id": int(row["plan_id"]),
        "version_n": int(row["version_n"]),
        "created_at": row["created_at"],
        "committed_at": row["committed_at"],
        "note": row["note"],
        "author": row["author"],
        "race_date": row["race_date"],
        "distance_bucket": row["distance_bucket"],
        "goal_time_s": int(row["goal_time_s"]) if row["goal_time_s"] is not None else None,
    }


def _week_row(row: sqlite3.Row) -> PlanWeekRow:
    return {
        "version_id": int(row["version_id"]),
        "week_start": row["week_start"],
        "target_miles": float(row["target_miles"]) if row["target_miles"] is not None else None,
        "target_miles_hi": float(row["target_miles_hi"]) if row["target_miles_hi"] is not None else None,
        "target_workouts": int(row["target_workouts"]),
        "target_long_run_miles": (
            float(row["target_long_run_miles"]) if row["target_long_run_miles"] is not None else None
        ),
        "target_long_run_minutes": (
            float(row["target_long_run_minutes"]) if row["target_long_run_minutes"] is not None else None
        ),
        "phase": row["phase"],
        "note": row["note"],
    }


def _day_row(row: sqlite3.Row) -> PlanDayRow:
    return {
        "version_id": int(row["version_id"]),
        "date": row["date"],
        "seq": int(row["seq"]),
        "slot": row["slot"],
        "title": row["title"],
        "target_miles": float(row["target_miles"]) if row["target_miles"] is not None else None,
        "target_json": row["target_json"],
        "terrain": row["terrain"],
        "note": row["note"],
        "target_minutes": float(row["target_minutes"]) if row["target_minutes"] is not None else None,
    }


GOAL_COLS = "key, horizon, type, params, target, why, overrides, flag, aggregate, threshold, from_week, to_week"


def _goal_row(row: sqlite3.Row) -> PlanGoalRow:
    return {
        "key": row["key"],
        "horizon": row["horizon"],
        "type": row["type"],
        "params": json.loads(row["params"]),
        "target": float(row["target"]),
        "why": row["why"],
        "overrides": json.loads(row["overrides"]),
        "flag": bool(row["flag"]),
        "aggregate": row["aggregate"],
        "threshold": float(row["threshold"]) if row["threshold"] is not None else None,
        "from_week": row["from_week"],
        "to_week": row["to_week"],
    }


def _version_goals(conn: sqlite3.Connection, version_id: int) -> list[PlanGoalRow]:
    rows = conn.execute(f"SELECT {GOAL_COLS} FROM plan_goals WHERE version_id = ? ORDER BY rowid", [version_id])
    return [_goal_row(r) for r in rows]


_VERSION_COLS = "version_id, plan_id, version_n, created_at, committed_at, note, author, " + ", ".join(_RACE_FIELDS)

# A plan's race and week span come from its latest committed version, or its
# draft when it has never been committed.
_PLAN_SELECT = f"""
    SELECT p.plan_id, p.title, p.status, p.created_at,
           v.race_date, v.distance_bucket, v.goal_time_s,
           (SELECT MIN(week_start) FROM plan_weeks WHERE version_id = v.version_id) AS first_week,
           (SELECT MAX(week_start) FROM plan_weeks WHERE version_id = v.version_id) AS last_week
    FROM plans p LEFT JOIN plan_versions v ON v.version_id = (
        SELECT version_id FROM plan_versions WHERE plan_id = p.plan_id
        ORDER BY committed_at IS NULL, version_n DESC LIMIT 1)
"""


def view_bundle(conn: sqlite3.Connection, plan_id: int) -> PlanVersionBundle | None:
    """The version a plan is shown as: its latest committed version, or its
    draft when it was never committed."""
    row = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? ORDER BY committed_at IS NULL, version_n DESC LIMIT 1",
        [plan_id],
    ).fetchone()
    return get_version(conn, int(row["version_id"])) if row is not None else None


def list_versions(conn: sqlite3.Connection, plan_id: int) -> list[PlanVersionRow]:
    """Every version of a plan, draft included, oldest first."""
    rows = conn.execute(f"SELECT {_VERSION_COLS} FROM plan_versions WHERE plan_id = ? ORDER BY version_n", [plan_id])
    return [_version_row(r) for r in rows]


def get_plan(conn: sqlite3.Connection, plan_id: int) -> PlanRow | None:
    row = conn.execute(_PLAN_SELECT + " WHERE p.plan_id = ?", [plan_id]).fetchone()
    return _plan_row(row) if row is not None else None


def list_plans(conn: sqlite3.Connection, where: str = "1") -> list[PlanRow]:
    """Plans matching a SQL condition on p (status), earliest weeks first;
    never-authored drafts last."""
    rows = [_plan_row(r) for r in conn.execute(_PLAN_SELECT + f" WHERE {where}")]
    return sorted(rows, key=lambda p: (p["first_week"] is None, p["first_week"] or "", p["plan_id"]))


# --- roles (derived from dates, never stored) --------------------------------

def plan_role(p: PlanRow, today: date) -> str:
    """current | next | past | candidate | abandoned. A committed plan is
    current while its weeks contain this week's Monday, next when they start
    later, past once they have ended; a never-committed draft is a candidate."""
    if p["status"] == "draft":
        return "candidate"
    if p["status"] == "abandoned":
        return "abandoned"
    monday = monday_of(today).isoformat()
    first, last = p["first_week"], p["last_week"]
    if first is None or last is None or last < monday:
        return "past"
    return "next" if first > monday else "current"


def current_plan(conn: sqlite3.Connection, today: date) -> PlanRow | None:
    """The committed, non-abandoned plan whose weeks contain this week."""
    return next((p for p in list_plans(conn) if plan_role(p, today) == "current"), None)


def default_plan(conn: sqlite3.Connection, today: date) -> PlanRow | None:
    """The plan a page opens on: the current one; in a gap between cycles the
    most recent past plan, else the earliest next one."""
    plans = list_plans(conn)
    past = [p for p in plans if plan_role(p, today) == "past"]
    upcoming = [p for p in plans if plan_role(p, today) == "next"]
    return current_plan(conn, today) or (past[-1] if past else upcoming[0] if upcoming else None)


# --- accessors ---------------------------------------------------------------


def get_version(conn: sqlite3.Connection, version_id: int) -> PlanVersionBundle | None:
    """A full version snapshot (version row + its weeks + its days), or None."""
    vrow = conn.execute(
        f"SELECT {_VERSION_COLS} FROM plan_versions WHERE version_id = ?",
        [version_id],
    ).fetchone()
    if vrow is None:
        return None
    weeks = [
        _week_row(r) for r in conn.execute(
            "SELECT version_id, week_start, target_miles, target_miles_hi, target_workouts, "
            "target_long_run_miles, target_long_run_minutes, phase, note "
            "FROM plan_weeks WHERE version_id = ? ORDER BY week_start",
            [version_id],
        ).fetchall()
    ]
    days = [
        _day_row(r) for r in conn.execute(
            "SELECT version_id, date, seq, slot, title, target_miles, target_json, "
            "terrain, note, target_minutes "
            "FROM plan_days WHERE version_id = ? ORDER BY date, seq",
            [version_id],
        ).fetchall()
    ]
    return {"version": _version_row(vrow), "weeks": weeks, "days": days, "goals": _version_goals(conn, version_id)}


def current_version_for_week(
    conn: sqlite3.Connection, plan_id: int, week_start: date
) -> PlanVersionBundle | None:
    """The version that governs week_start: the latest COMMITTED version whose
    committed_at is strictly before week_start, with a floor — version 1
    governs from the plan's first week regardless of its own committed_at.
    Drafts (committed_at IS NULL) are never eligible. Returns None when
    week_start precedes the plan's first week, or the plan has no committed
    versions yet."""
    if week_start.weekday() != 0:
        raise PlanValidationError(f"week_start {week_start.isoformat()} is not a Monday")

    versions = [
        _version_row(r) for r in conn.execute(
            f"SELECT {_VERSION_COLS} FROM plan_versions "
            "WHERE plan_id = ? AND committed_at IS NOT NULL ORDER BY version_n",
            [plan_id],
        ).fetchall()
    ]
    if not versions:
        return None

    v1 = versions[0]
    first_week_row = conn.execute(
        "SELECT MIN(week_start) AS d FROM plan_weeks WHERE version_id = ?", [v1["version_id"]]
    ).fetchone()
    first_week: str | None = first_week_row["d"] if first_week_row is not None else None
    if first_week is None or week_start.isoformat() < first_week:
        return None

    week_iso = week_start.isoformat()
    eligible = [
        v for v in versions
        if v["version_n"] == 1 or (v.get("committed_at") or "")[:10] < week_iso
    ]
    if not eligible:
        return None
    chosen = max(eligible, key=lambda v: v["version_n"])
    return get_version(conn, chosen["version_id"])


# --- completion (auto-complete + retrospective support) ---------------------

def find_completing_race(conn: sqlite3.Connection, plan_row: PlanRow) -> RaceMatch | None:
    """The synced effective-race activity that would complete plan_row (or
    already did): date within RACE_MATCH_WINDOW_DAYS of race_date, and
    classify_race_distance(distance_m) equal to distance_bucket
    case-insensitively (the codebase mixes 'Marathon'/'marathon' casing
    across distance_builds.py's Bucket and races.py's classify_race_distance
    — compare with .casefold() rather than assume either convention). Ties
    (more than one qualifying race in the window) prefer the one closest to
    race_date. Used by auto_complete_plan, the retrospective API, and
    completed_plans_by_race_date."""
    if plan_row["race_date"] is None or plan_row["distance_bucket"] is None:
        return None
    race_dt = date.fromisoformat(plan_row["race_date"])
    window_start = (race_dt - timedelta(days=RACE_MATCH_WINDOW_DAYS)).isoformat()
    window_end = (race_dt + timedelta(days=RACE_MATCH_WINDOW_DAYS)).isoformat()
    effective = effective_run_type_sql()
    rows = conn.execute(f"""
        SELECT activity_id, name, DATE(start_date) AS date, distance_m, moving_time_s
        FROM activities
        WHERE {effective} = 'race' AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        ORDER BY date
    """, [window_start, window_end]).fetchall()

    target_bucket = plan_row["distance_bucket"].strip().casefold()
    candidates = [
        r for r in rows
        if (classify_race_distance(r["distance_m"]) or "").casefold() == target_bucket
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: abs((date.fromisoformat(r["date"]) - race_dt).days))
    return RaceMatch(
        activity_id=int(best["activity_id"]),
        name=best["name"],
        date=best["date"],
        distance_m=best["distance_m"],
        moving_time_s=best["moving_time_s"],
    )


def auto_complete_plan(conn: sqlite3.Connection) -> list[int]:
    """Flips every active plan to 'completed' whose own race now has a
    matching synced effective-race activity (see find_completing_race) —
    called from sync.py after each sync's upserts. Plans without a race, and
    plans already completed, are never considered. The status flip is the
    only mutation — plans/plan_versions/plan_weeks/plan_days are untouched
    and stay append-only. Returns the completed plan_ids."""
    done = [
        p["plan_id"] for p in list_plans(conn, "p.status = 'active'")
        if find_completing_race(conn, p) is not None
    ]
    for plan_id in done:
        conn.execute("UPDATE plans SET status = 'completed' WHERE plan_id = ?", [plan_id])
    conn.commit()
    return done


def completed_plans_by_race_date(conn: sqlite3.Connection) -> dict[str, PlanRow]:
    """Every completed plan, keyed by the exact synced date of its matching
    race (find_completing_race) — the join surface builds.html/races.html use
    to cross-link a race/build row to its plan's retrospective (both already
    key their own rows by that same activity date). A completed plan whose
    race was never synced, or no longer matches, is simply absent."""
    out: dict[str, PlanRow] = {}
    for plan_row in list_plans(conn, "p.status = 'completed'"):
        match = find_completing_race(conn, plan_row)
        if match is not None:
            out[match["date"]] = plan_row
    return out


def _validate_day_target(day: DayInput, target: DayTarget) -> DayTarget:
    """Pure field-level validation of one day's target — no conn, no
    resolution. Shared by _freeze_day_target (committed versions: also
    resolves zone_name via a live fitness estimate) and upsert_draft_days
    (drafts: stores zone_name unresolved, resolved later at commit)."""
    day_label = f"day {day['date']} ({day.get('title') or day['slot']})"
    validated = cast(DayTarget, dict(target))

    reps = validated.get("reps")
    if reps is not None and reps < 0:
        raise PlanValidationError(f"{day_label}: target reps must be non-negative")
    reps_lo = validated.get("reps_lo")
    reps_hi = validated.get("reps_hi")
    if reps_lo is not None and reps_lo < 0:
        raise PlanValidationError(f"{day_label}: target reps_lo must be non-negative")
    if reps_hi is not None and reps_hi < 0:
        raise PlanValidationError(f"{day_label}: target reps_hi must be non-negative")
    if reps_lo is not None and reps_hi is not None and reps_lo > reps_hi:
        raise PlanValidationError(f"{day_label}: target reps_lo must be <= reps_hi")
    rep_duration_s = validated.get("rep_duration_s")
    if rep_duration_s is not None and rep_duration_s < 0:
        raise PlanValidationError(f"{day_label}: target rep_duration_s must be non-negative")
    rep_distance_m = validated.get("rep_distance_m")
    if rep_distance_m is not None and rep_distance_m < 0:
        raise PlanValidationError(f"{day_label}: target rep_distance_m must be non-negative")

    hr_lo = validated.get("hr_lo")
    hr_hi = validated.get("hr_hi")
    if hr_lo is not None and hr_lo < 0:
        raise PlanValidationError(f"{day_label}: target hr_lo must be non-negative")
    if hr_hi is not None and hr_hi < 0:
        raise PlanValidationError(f"{day_label}: target hr_hi must be non-negative")
    if hr_lo is not None and hr_hi is not None and hr_lo > hr_hi:
        raise PlanValidationError(f"{day_label}: target hr_lo must be <= hr_hi")

    zone_name = validated.get("zone_name")
    if zone_name is not None and zone_name not in ZONE_NAMES:
        raise PlanValidationError(f"{day_label}: zone_name {zone_name!r} must be one of {ZONE_NAMES}")

    pace_lo = validated.get("pace_lo")
    pace_hi = validated.get("pace_hi")
    if pace_lo is not None or pace_hi is not None:
        if pace_lo is None or pace_hi is None:
            raise PlanValidationError(f"{day_label}: pace_lo and pace_hi must both be given together")
        if pace_lo < 0 or pace_hi < 0:
            raise PlanValidationError(f"{day_label}: pace_lo/pace_hi must be non-negative")
        if pace_lo > pace_hi:
            raise PlanValidationError(f"{day_label}: pace_lo must be <= pace_hi")

    return validated


def _freeze_day_target(
    conn: sqlite3.Connection, day: DayInput, target: DayTarget, creation_date: date
) -> DayTarget:
    """Validates (see _validate_day_target) and freezes one day's target for a
    committed version. Explicit pace_lo/pace_hi are always allowed as-is. A
    zone_name with no explicit paces is resolved via a live
    estimate_fitness(as_of=creation_date) + zones_from_predicted: 'easy'
    freezes to the easy_range band, the other four zones freeze to their
    fitness.py anchor +/- that zone's tolerance. Rejects (never stores
    unresolved) when no fitness estimate is computable."""
    day_label = f"day {day['date']} ({day.get('title') or day['slot']})"
    frozen = _validate_day_target(day, target)

    if frozen.get("pace_lo") is not None or frozen.get("pace_hi") is not None:
        return frozen  # explicit paces always allowed, no resolution needed

    zone_name = frozen.get("zone_name")
    if zone_name is None:
        return frozen  # no pace info at all (rep-only or HR-only sketch) — fine

    est = estimate_fitness(conn, creation_date)
    if est is None:
        raise PlanValidationError(
            f"{day_label}: cannot freeze zone-anchored target '{zone_name}' — no fitness "
            f"estimate is computable as of {creation_date.isoformat()}; provide explicit "
            "pace_lo/pace_hi instead"
        )
    zones = zones_from_predicted(est["predicted"]["marathon"], est["predicted"]["5K"])
    if zone_name == "easy":
        mp = float(zones["marathon"])
        frozen["pace_lo"] = round(mp + EASY_LO_OFFSET_MIN, 2)
        frozen["pace_hi"] = round(mp + EASY_HI_OFFSET_MIN, 2)
    else:
        anchor = float(zones[zone_name])
        tol = _ZONE_TOLERANCE[zone_name]
        frozen["pace_lo"] = round(anchor * (1 - tol), 2)
        frozen["pace_hi"] = round(anchor * (1 + tol), 2)
    return frozen


def _validate_week_fields(w: WeekInput) -> None:
    """Field-level checks for one week input. Structural checks that need the
    whole set (Mondays relative to each other, contiguity, ending at the race
    week) are commit_plan's job — a single week never has enough context for
    those."""
    lo = w.get("target_miles")
    if lo is not None and lo < 0:
        raise PlanValidationError(f"week {w['week_start']}: target_miles must be non-negative")
    hi = w.get("target_miles_hi")
    if hi is not None:
        if hi < 0:
            raise PlanValidationError(f"week {w['week_start']}: target_miles_hi must be non-negative")
        if lo is None:
            raise PlanValidationError(
                f"week {w['week_start']}: target_miles_hi given without target_miles (the range floor)"
            )
        if hi < lo:
            raise PlanValidationError(f"week {w['week_start']}: target_miles_hi must be >= target_miles")
    if w["target_workouts"] < 0:
        raise PlanValidationError(f"week {w['week_start']}: target_workouts must be non-negative")
    long_run = w.get("target_long_run_miles")
    if long_run is not None and long_run < 0:
        raise PlanValidationError(f"week {w['week_start']}: target_long_run_miles must be non-negative")
    long_run_min = w.get("target_long_run_minutes")
    if long_run_min is not None and long_run_min < 0:
        raise PlanValidationError(
            f"week {w['week_start']}: target_long_run_minutes must be non-negative"
        )
    if w["phase"] not in PHASES:
        raise PlanValidationError(f"week {w['week_start']}: phase {w['phase']!r} must be one of {PHASES}")


def _validate_day_fields(d: DayInput) -> None:
    """Field-level checks for one day input (excluding target, validated
    separately by _validate_day_target/_freeze_day_target)."""
    if d["slot"] not in SLOTS:
        raise PlanValidationError(f"day {d['date']}: slot {d['slot']!r} must be one of {SLOTS}")
    seq = d.get("seq", 1)
    if seq < 1:
        raise PlanValidationError(f"day {d['date']}: seq must be >= 1")
    target_miles = d.get("target_miles")
    if target_miles is not None and target_miles < 0:
        raise PlanValidationError(f"day {d['date']}: target_miles must be non-negative")
    target_minutes = d.get("target_minutes")
    if target_minutes is not None and target_minutes < 0:
        raise PlanValidationError(f"day {d['date']}: target_minutes must be non-negative")
    terrain = d.get("terrain")
    if terrain is not None and terrain not in TERRAIN_VALUES:
        raise PlanValidationError(f"day {d['date']}: terrain {terrain!r} must be one of {TERRAIN_VALUES}")


def add_log_entry(
    conn: sqlite3.Connection,
    plan_id: int,
    *,
    log_date: str,
    action: Literal["skipped", "moved", "modified", "note"],
    reason: str | None = None,
) -> int:
    """Records day-level reality ("skipped Tue, slept badly") without touching
    the plan or bumping a version."""
    if get_plan(conn, plan_id) is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    try:
        date.fromisoformat(log_date)
    except ValueError as e:
        raise PlanValidationError(f"log date {log_date!r} is not a valid ISO date") from e
    if action not in LOG_ACTIONS:
        raise PlanValidationError(f"log action {action!r} must be one of {LOG_ACTIONS}")

    created_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO plan_log (plan_id, date, action, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (plan_id, log_date, action, reason, created_at),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# --- draft lifecycle ---------------------------------------------------------
#
# Exactly one mutable thing: a plan_versions row with committed_at IS NULL.
# Its weeks/days may be inserted, updated, and deleted freely, in any order,
# with gaps (upsert_draft_weeks/upsert_draft_days/delete_draft_*). commit_plan
# runs the global validation (Mondays, contiguous, covering the race week, no
# overlap with another committed plan),
# re-freezes zone-anchored day targets as of the commit date, stamps
# committed_at on the SAME version_id, and the version becomes immutable — no
# new version_id is allocated at commit, only at draft creation
# (start_plan_draft / start_revision_draft).


def require_draft_version(conn: sqlite3.Connection, version_id: int) -> int:
    """Returns the version's plan_id if it exists and is still a draft
    (committed_at IS NULL); raises otherwise. Every draft-mutating function
    checks this first — a committed version is never writable."""
    row = conn.execute(
        "SELECT plan_id, committed_at FROM plan_versions WHERE version_id = ?", [version_id]
    ).fetchone()
    if row is None:
        raise PlanValidationError(f"version {version_id} does not exist")
    if row["committed_at"] is not None:
        raise PlanValidationError(
            f"version {version_id} is already committed; only a draft version can be edited"
        )
    return int(row["plan_id"])


def plan_start_monday(conn: sqlite3.Connection, plan_id: int) -> date | None:
    """The plan's first week (from version 1, which always governs from the
    plan's start regardless of its own committed_at), or None if v1 has no
    weeks yet."""
    row = conn.execute("""
        SELECT MIN(pw.week_start) AS d
        FROM plan_weeks pw JOIN plan_versions pv ON pv.version_id = pw.version_id
        WHERE pv.plan_id = ? AND pv.version_n = 1
    """, [plan_id]).fetchone()
    return date.fromisoformat(row["d"]) if row is not None and row["d"] is not None else None


def _validate_race(race_date: str | None, distance_bucket: str | None, goal_time_s: int | None) -> None:
    """A race is optional, but its date and distance come together and a goal
    time needs a race."""
    if race_date is None:
        if distance_bucket is not None or goal_time_s is not None:
            raise PlanValidationError("distance_bucket and goal_time_s need a race_date")
        return
    try:
        date.fromisoformat(race_date)
    except ValueError as e:
        raise PlanValidationError(f"race_date {race_date!r} is not a valid ISO date") from e
    if distance_bucket is None or not distance_bucket.strip():
        raise PlanValidationError("distance_bucket must not be empty when race_date is set")
    if goal_time_s is not None and goal_time_s < 0:
        raise PlanValidationError(f"goal_time_s must be non-negative, got {goal_time_s}")


def set_draft_race(
    conn: sqlite3.Connection,
    version_id: int,
    race_date: str | None,
    distance_bucket: str | None = None,
    goal_time_s: int | None = None,
) -> None:
    """Sets the draft's race, or clears it when race_date is None."""
    require_draft_version(conn, version_id)
    _validate_race(race_date, distance_bucket, goal_time_s)
    conn.execute(
        "UPDATE plan_versions SET race_date = ?, distance_bucket = ?, goal_time_s = ? WHERE version_id = ?",
        (race_date, distance_bucket, goal_time_s, version_id),
    )
    conn.commit()


def start_plan_draft(
    conn: sqlite3.Connection,
    *,
    title: str,
    race_date: str | None = None,
    distance_bucket: str | None = None,
    goal_time_s: int | None = None,
) -> tuple[int, int]:
    """Creates a new plan, status='draft', plus its empty first (draft)
    version. Any number of draft plans may coexist with each other and with
    committed ones; commit_plan is where overlap is rejected. Returns
    (plan_id, version_id)."""
    if not title.strip():
        raise PlanValidationError("plan title must not be empty")
    _validate_race(race_date, distance_bucket, goal_time_s)

    created_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO plans (title, status, created_at) VALUES (?, 'draft', ?)", (title, created_at)
    )
    assert cur.lastrowid is not None
    plan_id = int(cur.lastrowid)

    vcur = conn.execute(
        "INSERT INTO plan_versions (plan_id, version_n, created_at, committed_at, note, author, "
        "race_date, distance_bucket, goal_time_s) VALUES (?, 1, ?, NULL, NULL, 'agent', ?, ?, ?)",
        (plan_id, created_at, race_date, distance_bucket, goal_time_s),
    )
    assert vcur.lastrowid is not None
    conn.commit()
    return plan_id, int(vcur.lastrowid)


def start_revision_draft(conn: sqlite3.Connection, plan_id: int) -> int:
    """Seeds a new draft version for plan_id by copying the current (latest
    committed) version's race and weeks/days verbatim — the athlete then edits
    incrementally via upsert_draft_weeks/upsert_draft_days. Rejects a plan
    with no committed version yet (nothing to revise — use start_plan_draft)
    or one that already has a draft in progress (one draft per plan).
    Returns the new draft's version_id."""
    if get_plan(conn, plan_id) is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")

    existing_draft = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if existing_draft is not None:
        raise PlanValidationError(
            f"plan {plan_id} already has a draft in progress (version_id="
            f"{int(existing_draft['version_id'])}); commit or discard it before starting another"
        )

    latest = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NOT NULL "
        "ORDER BY version_n DESC LIMIT 1",
        [plan_id],
    ).fetchone()
    if latest is None:
        raise PlanValidationError(f"plan {plan_id} has no committed version yet to revise")
    source = get_version(conn, int(latest["version_id"]))
    assert source is not None
    race = source["version"]

    version_n_row = conn.execute(
        "SELECT COALESCE(MAX(version_n), 0) AS n FROM plan_versions WHERE plan_id = ?", [plan_id]
    ).fetchone()
    version_n = int(version_n_row["n"]) + 1
    created_at = datetime.now(timezone.utc).isoformat()
    vcur = conn.execute(
        "INSERT INTO plan_versions (plan_id, version_n, created_at, committed_at, note, author, "
        "race_date, distance_bucket, goal_time_s) VALUES (?, ?, ?, NULL, NULL, 'agent', ?, ?, ?)",
        (plan_id, version_n, created_at, race["race_date"], race["distance_bucket"], race["goal_time_s"]),
    )
    assert vcur.lastrowid is not None
    version_id = int(vcur.lastrowid)

    conn.executemany(
        "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, "
        "target_workouts, target_long_run_miles, target_long_run_minutes, "
        "phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (version_id, w["week_start"], w["target_miles"], w["target_miles_hi"], w["target_workouts"],
             w["target_long_run_miles"], w["target_long_run_minutes"],
             w["phase"], w["note"])
            for w in source["weeks"]
        ],
    )
    conn.executemany(
        "INSERT INTO plan_days (version_id, date, seq, slot, title, target_miles, target_json, "
        "terrain, note, target_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (version_id, d["date"], d["seq"], d["slot"], d["title"], d["target_miles"], d["target_json"],
             d["terrain"], d["note"], d["target_minutes"])
            for d in source["days"]
        ],
    )
    conn.execute(
        f"INSERT INTO plan_goals (version_id, {GOAL_COLS}) "
        f"SELECT ?, {GOAL_COLS} FROM plan_goals WHERE version_id = ?",
        [version_id, source["version"]["version_id"]],
    )
    conn.commit()
    return version_id


def upsert_draft_weeks(conn: sqlite3.Connection, version_id: int, weeks: list[WeekInput]) -> None:
    """Inserts or updates (by week_start) any subset of a draft's weeks —
    call it in batches as the athlete approves each block. Local validation
    only (Monday-aligned, non-negative, known phase, hi >= lo); contiguity and
    reaching the race week are commit_plan's job, not this one's, since a
    partial batch is expected to have gaps mid-authoring."""
    require_draft_version(conn, version_id)
    if not weeks:
        return

    parsed: list[tuple[date, WeekInput]] = []
    for w in weeks:
        try:
            wd = date.fromisoformat(w["week_start"])
        except ValueError as e:
            raise PlanValidationError(f"week_start {w['week_start']!r} is not a valid ISO date") from e
        if wd.weekday() != 0:
            raise PlanValidationError(f"week {w['week_start']} is not a Monday")
        _validate_week_fields(w)
        parsed.append((wd, w))

    conn.executemany(
        "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, "
        "target_workouts, target_long_run_miles, target_long_run_minutes, "
        "phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(version_id, week_start) DO UPDATE SET "
        "target_miles = excluded.target_miles, target_miles_hi = excluded.target_miles_hi, "
        "target_workouts = excluded.target_workouts, "
        "target_long_run_miles = excluded.target_long_run_miles, "
        "target_long_run_minutes = excluded.target_long_run_minutes, "
        "phase = excluded.phase, note = excluded.note",
        [
            (version_id, wd.isoformat(), w.get("target_miles"), w.get("target_miles_hi"), w["target_workouts"],
             w.get("target_long_run_miles"), w.get("target_long_run_minutes"),
             w["phase"], w.get("note"))
            for wd, w in parsed
        ],
    )
    conn.commit()


def upsert_draft_days(conn: sqlite3.Connection, version_id: int, days: list[DayInput]) -> None:
    """Inserts or updates (by date, seq) any subset of a draft's days. Target
    validation is local only (_validate_day_target): a zone-anchored target
    is accepted with zone_name set and no pace_lo/pace_hi — it stays
    unresolved until commit_plan freezes it. Day-to-week membership is not
    checked here (a day may be authored before its week row exists)."""
    require_draft_version(conn, version_id)
    if not days:
        return

    rows: list[tuple[object, ...]] = []
    for d in days:
        try:
            date.fromisoformat(d["date"])
        except ValueError as e:
            raise PlanValidationError(f"day date {d['date']!r} is not a valid ISO date") from e
        _validate_day_fields(d)
        target = d.get("target")
        validated_target = _validate_day_target(d, target) if target else None
        rows.append((
            version_id, d["date"], d.get("seq", 1), d["slot"], d.get("title"),
            d.get("target_miles"), json.dumps(validated_target) if validated_target else None,
            d.get("terrain"), d.get("note"), d.get("target_minutes"),
        ))

    conn.executemany(
        "INSERT INTO plan_days (version_id, date, seq, slot, title, target_miles, target_json, "
        "terrain, note, target_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(version_id, date, seq) DO UPDATE SET "
        "slot = excluded.slot, title = excluded.title, target_miles = excluded.target_miles, "
        "target_json = excluded.target_json, terrain = excluded.terrain, note = excluded.note, "
        "target_minutes = excluded.target_minutes",
        rows,
    )
    conn.commit()


def delete_draft_weeks(conn: sqlite3.Connection, version_id: int, week_starts: list[str]) -> int:
    """Deletes the given week_starts from a draft (and any days that fall in
    them, so a deleted week doesn't leave orphan day rows). Returns the
    number of week rows deleted. No-ops on week_starts not present."""
    require_draft_version(conn, version_id)
    deleted = 0
    for ws in week_starts:
        wd = date.fromisoformat(ws)
        week_end = (wd + timedelta(days=6)).isoformat()
        conn.execute(
            "DELETE FROM plan_days WHERE version_id = ? AND date >= ? AND date <= ?",
            (version_id, ws, week_end),
        )
        cur = conn.execute(
            "DELETE FROM plan_weeks WHERE version_id = ? AND week_start = ?", (version_id, ws)
        )
        deleted += cur.rowcount
    conn.commit()
    return deleted


def delete_draft_days(
    conn: sqlite3.Connection, version_id: int, dates_or_keys: list[str | tuple[str, int]]
) -> int:
    """Deletes days from a draft. A plain date string deletes every seq for
    that date; a (date, seq) tuple deletes just that one. Returns the number
    of day rows deleted."""
    require_draft_version(conn, version_id)
    deleted = 0
    for key in dates_or_keys:
        if isinstance(key, tuple):
            day_date, seq = key
            cur = conn.execute(
                "DELETE FROM plan_days WHERE version_id = ? AND date = ? AND seq = ?",
                (version_id, day_date, seq),
            )
        else:
            cur = conn.execute(
                "DELETE FROM plan_days WHERE version_id = ? AND date = ?", (version_id, key)
            )
        deleted += cur.rowcount
    conn.commit()
    return deleted


def _compress_ranges(indices: list[int]) -> list[tuple[int, int]]:
    """Sorted ints -> maximal contiguous (start, end) inclusive runs."""
    if not indices:
        return []
    ranges: list[tuple[int, int]] = []
    start = prev = indices[0]
    for i in indices[1:]:
        if i == prev + 1:
            prev = i
            continue
        ranges.append((start, prev))
        start = prev = i
    ranges.append((start, prev))
    return ranges


def _fmt_mi(miles: float) -> str:
    """%g-style mileage for messages: strips a trailing .0 (40 not 40.0) but
    keeps meaningful decimals (38.5)."""
    return f"{miles:g}"


def compute_week_mileage(weeks: list[PlanWeekRow], days: list[PlanDayRow]) -> list[WeekMileageCheck]:
    """Single source of truth for week-vs-day mileage consistency: sums each
    week's mileage-slot (MILEAGE_SLOTS) day targets and compares the sum
    against that week's authored target_miles/target_miles_hi band, within
    WEEK_MILEAGE_TOLERANCE_MI. A day belongs to the week whose Monday equals
    its date minus its weekday offset. A deliberately unspecified week
    (target_miles is None), a week with no mileage-slot days authored yet, or
    a lower-bound sum (some days missing target_miles, e.g. duration-only)
    that hasn't already breached the ceiling all report consistent=True —
    there's nothing to contradict yet. Never raises; commit_plan is the only
    caller that turns an inconsistent entry into a hard rejection."""
    days_by_week: dict[str, list[PlanDayRow]] = {}
    for d in days:
        dd = date.fromisoformat(d["date"])
        wk = monday_of(dd).isoformat()
        days_by_week.setdefault(wk, []).append(d)

    out: list[WeekMileageCheck] = []
    for w in sorted(weeks, key=lambda w: w["week_start"]):
        week_start = w["week_start"]
        mileage_days = [d for d in days_by_week.get(week_start, []) if d["slot"] in MILEAGE_SLOTS]
        day_miles_total = round(sum(d["target_miles"] or 0.0 for d in mileage_days), 1)
        days_missing_miles = sum(1 for d in mileage_days if d["target_miles"] is None)

        target_miles = w["target_miles"]
        target_miles_hi = w["target_miles_hi"]
        band_lo = target_miles
        band_hi = target_miles_hi if target_miles_hi is not None else target_miles

        consistent = True
        message: str | None = None

        if band_lo is None or band_hi is None:
            pass  # deliberately unspecified week — nothing authored to check against
        elif not mileage_days:
            pass  # nothing authored to sum yet
        elif days_missing_miles > 0:
            # sum is a lower bound only; the floor isn't verifiable, but an
            # already-over-ceiling lower bound is a real contradiction.
            if day_miles_total > band_hi + WEEK_MILEAGE_TOLERANCE_MI:
                consistent = False
                message = (
                    f"week {week_start}: days already sum to {_fmt_mi(day_miles_total)} mi with "
                    f"{days_missing_miles} day(s) lacking a mileage figure, over the week ceiling "
                    f"of {_fmt_mi(band_hi)} mi"
                )
        else:
            if day_miles_total < band_lo - WEEK_MILEAGE_TOLERANCE_MI or (
                day_miles_total > band_hi + WEEK_MILEAGE_TOLERANCE_MI
            ):
                consistent = False
                band_str = (
                    f"{_fmt_mi(band_lo)}–{_fmt_mi(band_hi)} mi" if band_hi != band_lo
                    else f"{_fmt_mi(band_lo)} mi"
                )
                message = (
                    f"week {week_start}: days sum to {_fmt_mi(day_miles_total)} mi, but the week "
                    f"targets {band_str}"
                )

        out.append({
            "week_start": week_start,
            "day_miles_total": day_miles_total,
            "days_missing_miles": days_missing_miles,
            "target_miles": target_miles,
            "target_miles_hi": target_miles_hi,
            "consistent": consistent,
            "message": message,
        })
    return out


def _draft_gap_report(version: PlanVersionRow, weeks: list[PlanWeekRow], days: list[PlanDayRow]) -> list[str]:
    """Plain-English messages naming what commit_plan's global validation
    would reject right now: unauthored weeks (by ordinal week number from the
    earliest authored week), days whose week has no week row, and weeks that
    miss the draft's race week. Never raises — this is a report, not a gate."""
    if not weeks:
        return ["no weeks authored yet"]

    week_starts = sorted(w["week_start"] for w in weeks)
    plan_start = date.fromisoformat(week_starts[0])
    race_monday = monday_of(date.fromisoformat(version["race_date"])) if version["race_date"] else None
    last_authored = date.fromisoformat(week_starts[-1])

    have = {date.fromisoformat(w) for w in week_starts}
    missing_idx: list[int] = []
    idx = 0
    cursor = plan_start
    while cursor <= last_authored:
        idx += 1
        if cursor not in have:
            missing_idx.append(idx)
        cursor += timedelta(weeks=1)

    gaps: list[str] = []
    for start_i, end_i in _compress_ranges(missing_idx):
        gaps.append(f"week {start_i} unauthored" if start_i == end_i else f"weeks {start_i}-{end_i} unauthored")

    if race_monday is not None and not plan_start <= race_monday <= last_authored:
        gaps.append(
            f"weeks don't include race week ({race_monday.isoformat()}); "
            f"authored weeks run {week_starts[0]} to {week_starts[-1]}"
        )

    week_start_set = set(week_starts)
    day_weeks = sorted({
        monday_of(date.fromisoformat(d["date"])).isoformat()
        for d in days
    })
    for wk in day_weeks:
        if wk not in week_start_set:
            gaps.append(f"week of {wk} has days but no week row")

    return gaps


def get_draft(conn: sqlite3.Connection, plan_id: int) -> DraftBundle:
    """Current state of plan_id's one draft (weeks/days as authored so far,
    zone-anchored day targets still unresolved) plus a gap report — see
    _draft_gap_report. Raises if the plan doesn't exist or has no draft."""
    plan = get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    draft = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if draft is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")

    bundle = get_version(conn, int(draft["version_id"]))
    assert bundle is not None
    gaps = _draft_gap_report(bundle["version"], bundle["weeks"], bundle["days"])
    return {
        "plan": plan, "version": bundle["version"], "weeks": bundle["weeks"], "days": bundle["days"],
        "goals": bundle["goals"], "gaps": gaps,
    }


def _snapshot_past_weeks(conn: sqlite3.Connection, plan_id: int, version_id: int) -> list[str]:
    """Past-week protection for a revision draft, ported from the legacy
    revise_training_plan tool so it has one home: any week whose Monday is on
    or before the start of the current week — already governing or already in
    progress — is overwritten in place with whatever version actually
    governed it at the time, regardless of what the draft proposed for that
    week/its days. Makes rewriting history to look adherent structurally
    impossible no matter which write path produced the draft. Returns the
    week_starts that were overridden this way."""
    this_monday = monday_of(date.today())
    plan_start = plan_start_monday(conn, plan_id)
    if plan_start is None:
        return []

    preserved: list[str] = []
    m = plan_start
    while m <= this_monday:
        governing = current_version_for_week(conn, plan_id, m)
        iso = m.isoformat()
        week_row = next((w for w in governing["weeks"] if w["week_start"] == iso), None) if governing else None
        if governing is not None and week_row is not None:  # a week added by the revision rewrites no history
            week_end = (m + timedelta(days=6)).isoformat()
            conn.execute(
                "DELETE FROM plan_days WHERE version_id = ? AND date >= ? AND date <= ?",
                (version_id, iso, week_end),
            )
            conn.execute(
                "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, "
                "target_workouts, target_long_run_miles, target_long_run_minutes, "
                "phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(version_id, week_start) DO UPDATE SET "
                "target_miles = excluded.target_miles, target_miles_hi = excluded.target_miles_hi, "
                "target_workouts = excluded.target_workouts, "
                "target_long_run_miles = excluded.target_long_run_miles, "
                "target_long_run_minutes = excluded.target_long_run_minutes, "
                "phase = excluded.phase, note = excluded.note",
                (version_id, iso, week_row["target_miles"], week_row["target_miles_hi"],
                 week_row["target_workouts"], week_row["target_long_run_miles"],
                 week_row["target_long_run_minutes"],
                 week_row["phase"], week_row["note"]),
            )
            preserved.append(iso)
            for day_row in governing["days"]:
                if iso <= day_row["date"] <= week_end:
                    conn.execute(
                        "INSERT INTO plan_days (version_id, date, seq, slot, title, target_miles, "
                        "target_json, terrain, note, target_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (version_id, day_row["date"], day_row["seq"], day_row["slot"], day_row["title"],
                         day_row["target_miles"], day_row["target_json"], day_row["terrain"],
                         day_row["note"], day_row["target_minutes"]),
                    )
        m += timedelta(weeks=1)
    return preserved


def commit_plan(conn: sqlite3.Connection, plan_id: int, *, note: str) -> int:
    """Runs full global validation on plan_id's draft (Mondays already
    guaranteed by upsert; contiguous, includes the race week when the draft
    has a race (it may extend past it), shares no week with another
    committed, non-abandoned plan, every day falls
    in a committed week; every week's authored mileage band agrees with the
    program-computed sum of its mileage-slot days — see compute_week_mileage
    — within WEEK_MILEAGE_TOLERANCE_MI, the hard block this function adds),
    snapshots past weeks for a revision (see _snapshot_past_weeks — skipped
    for a plan's first-ever commit, since backdated week 1 is legitimately
    authored history, not something to protect from itself), re-freezes
    every zone-anchored day target as of today (freeze-at-commit — commit IS
    "authoring" for that rule), stamps committed_at on the draft's own
    version_id (no new version_id is allocated), and flips the plan to
    status='active'.
    """
    if not note or not note.strip():
        raise PlanValidationError("commit_plan requires a non-empty note")
    plan = get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")

    draft = conn.execute(
        "SELECT version_id, race_date FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if draft is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")
    version_id = int(draft["version_id"])

    has_prior_committed = conn.execute(
        "SELECT 1 FROM plan_versions WHERE plan_id = ? AND committed_at IS NOT NULL LIMIT 1", [plan_id]
    ).fetchone() is not None
    if has_prior_committed:
        _snapshot_past_weeks(conn, plan_id, version_id)

    weeks = [_week_row(r) for r in conn.execute(
        "SELECT version_id, week_start, target_miles, target_miles_hi, target_workouts, "
        "target_long_run_miles, target_long_run_minutes, phase, note "
        "FROM plan_weeks WHERE version_id = ? ORDER BY week_start",
        [version_id],
    ).fetchall()]
    if not weeks:
        raise PlanValidationError("cannot commit an empty draft — no weeks authored")

    for prev, cur_w in zip(weeks, weeks[1:]):
        gap = (date.fromisoformat(cur_w["week_start"]) - date.fromisoformat(prev["week_start"])).days
        if gap != 7:
            raise PlanValidationError(
                f"weeks are not contiguous: {prev['week_start']} to {cur_w['week_start']} "
                f"is {gap} days apart, expected 7"
            )

    first_week, last_week = weeks[0]["week_start"], weeks[-1]["week_start"]
    if draft["race_date"] is not None:
        race_monday = monday_of(date.fromisoformat(draft["race_date"])).isoformat()
        if not first_week <= race_monday <= last_week:
            raise PlanValidationError(
                f"plan weeks must include the race week ({race_monday}); weeks given run {first_week} to {last_week}"
            )

    for other in list_plans(conn, "p.status IN ('active', 'completed')"):
        if other["plan_id"] == plan_id or other["first_week"] is None or other["last_week"] is None:
            continue
        overlap_start, overlap_end = max(first_week, other["first_week"]), min(last_week, other["last_week"])
        if overlap_start <= overlap_end:
            raise PlanValidationError(
                f"weeks {overlap_start} to {overlap_end} are already covered by plan {other['plan_id']} "
                f"({other['title']!r}); a committed plan cannot share weeks with another"
            )

    week_starts = {w["week_start"] for w in weeks}
    days = [_day_row(r) for r in conn.execute(
        "SELECT version_id, date, seq, slot, title, target_miles, target_json, terrain, note, target_minutes "
        "FROM plan_days WHERE version_id = ? ORDER BY date, seq",
        [version_id],
    ).fetchall()]
    for d in days:
        dd = date.fromisoformat(d["date"])
        wk = monday_of(dd).isoformat()
        if wk not in week_starts:
            raise PlanValidationError(
                f"day {d['date']} falls in week {wk}, which is not among the committed weeks"
            )

    # goals imports this module.
    from .goals import validate_goals
    validate_goals(_version_goals(conn, version_id), week_starts)

    mileage_checks = compute_week_mileage(weeks, days)
    inconsistent = [c for c in mileage_checks if not c["consistent"]]
    if inconsistent:
        raise PlanValidationError(
            "plan is internally inconsistent: "
            + "; ".join(cast(str, c["message"]) for c in inconsistent)
        )

    creation_date = date.today()
    for d in days:
        if not d["target_json"]:
            continue
        target = cast(DayTarget, json.loads(d["target_json"]))
        if target.get("pace_lo") is None and target.get("zone_name") is not None:
            day_input: DayInput = {"date": d["date"], "slot": d["slot"], "title": d["title"]}
            frozen = _freeze_day_target(conn, day_input, target, creation_date)
            conn.execute(
                "UPDATE plan_days SET target_json = ? WHERE version_id = ? AND date = ? AND seq = ?",
                (json.dumps(frozen), version_id, d["date"], d["seq"]),
            )

    conn.execute(
        "UPDATE plan_versions SET committed_at = ?, note = ? WHERE version_id = ?",
        (datetime.now(timezone.utc).isoformat(), note, version_id),
    )
    if plan["status"] != "active":
        conn.execute("UPDATE plans SET status = 'active' WHERE plan_id = ?", [plan_id])
    conn.commit()
    return version_id


def discard_draft(conn: sqlite3.Connection, plan_id: int) -> None:
    """Deletes plan_id's draft version and its weeks/days. If the plan itself
    was never committed (status still 'draft'), the plan row is deleted too —
    a never-committed plan simply disappears."""
    plan = get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    draft = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if draft is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")
    version_id = int(draft["version_id"])

    conn.execute("DELETE FROM plan_days WHERE version_id = ?", [version_id])
    conn.execute("DELETE FROM plan_goals WHERE version_id = ?", [version_id])
    conn.execute("DELETE FROM plan_weeks WHERE version_id = ?", [version_id])
    conn.execute("DELETE FROM plan_versions WHERE version_id = ?", [version_id])
    if plan["status"] == "draft":
        conn.execute("DELETE FROM plan_docs WHERE plan_id = ?", [plan_id])
        conn.execute("DELETE FROM plans WHERE plan_id = ?", [plan_id])
    conn.commit()


def diff_versions(conn: sqlite3.Connection, version_a: int, version_b: int) -> VersionDiff:
    """Computed, never stored: changed weeks (which target fields changed) and
    added/removed/changed days and goals between two versions of the same plan."""
    a = get_version(conn, version_a)
    b = get_version(conn, version_b)
    if a is None:
        raise PlanValidationError(f"version {version_a} does not exist")
    if b is None:
        raise PlanValidationError(f"version {version_b} does not exist")

    race_a, race_b = cast(dict[str, object], a["version"]), cast(dict[str, object], b["version"])
    changed_race = {f: (race_a[f], race_b[f]) for f in _RACE_FIELDS if race_a[f] != race_b[f]}

    weeks_a = {w["week_start"]: w for w in a["weeks"]}
    weeks_b = {w["week_start"]: w for w in b["weeks"]}
    changed_weeks: list[WeekDiff] = []
    for week_start in sorted(set(weeks_a) | set(weeks_b)):
        wa = weeks_a.get(week_start)
        wb = weeks_b.get(week_start)
        if wa is None:
            changed_weeks.append({"week_start": week_start, "change": "added", "changed_fields": []})
        elif wb is None:
            changed_weeks.append({"week_start": week_start, "change": "removed", "changed_fields": []})
        else:
            wa_map = cast(dict[str, object], wa)
            wb_map = cast(dict[str, object], wb)
            fields = [f for f in _WEEK_DIFF_FIELDS if wa_map[f] != wb_map[f]]
            if fields:
                changed_weeks.append({"week_start": week_start, "change": "changed", "changed_fields": fields})

    days_a = {(d["date"], d["seq"]): d for d in a["days"]}
    days_b = {(d["date"], d["seq"]): d for d in b["days"]}
    changed_days: list[DayDiff] = []
    for key in sorted(set(days_a) | set(days_b)):
        day_date, seq = key
        da = days_a.get(key)
        db_ = days_b.get(key)
        if da is None:
            changed_days.append({"date": day_date, "seq": seq, "change": "added", "changed_fields": []})
        elif db_ is None:
            changed_days.append({"date": day_date, "seq": seq, "change": "removed", "changed_fields": []})
        else:
            da_map = cast(dict[str, object], da)
            db_map = cast(dict[str, object], db_)
            fields = [f for f in _DAY_DIFF_FIELDS if da_map[f] != db_map[f]]
            if fields:
                changed_days.append({"date": day_date, "seq": seq, "change": "changed", "changed_fields": fields})

    goals_a = {g["key"]: g for g in a["goals"]}
    goals_b = {g["key"]: g for g in b["goals"]}
    changed_goals: list[GoalDiff] = [
        {"key": key, "change": "added" if key not in goals_a else "removed" if key not in goals_b else "changed"}
        for key in sorted(set(goals_a) | set(goals_b))
        if goals_a.get(key) != goals_b.get(key)
    ]

    return {
        "version_a": version_a,
        "version_b": version_b,
        "changed_race": changed_race,
        "changed_weeks": changed_weeks,
        "changed_days": changed_days,
        "changed_goals": changed_goals,
    }
