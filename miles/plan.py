"""
Plan schema accessors: pure helpers over the plans/plan_versions/plan_weeks/
plan_days/plan_log tables (miles/db.py). Plans are athlete-authored ground
truth, like activities — not derived, exempt from derive_all.

Validation lives here, never in callers: week_starts must be Mondays,
contiguous, ending at the race week; targets non-negative; one active plan;
versions are immutable append-only snapshots (there is no UPDATE path).
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

from .db import PlanDayRow, PlanRow, PlanVersionRow, PlanWeekRow, effective_run_type_sql
from .fitness import (
    INTERVAL_TOLERANCE,
    MP_TOLERANCE,
    REPETITION_TOLERANCE,
    THRESHOLD_TOLERANCE,
    estimate_fitness,
    zones_from_predicted,
)
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

_WEEK_DIFF_FIELDS: tuple[str, ...] = (
    "target_miles", "target_workouts", "target_long_run_miles", "phase", "note",
)
_DAY_DIFF_FIELDS: tuple[str, ...] = ("slot", "title", "target_miles", "target_json")

# Window (inclusive, either side) around a plan's race_date within which a
# synced effective-race activity can auto-complete the plan — covers
# timezone edges where Strava's local start_date lands a day off race_date.
RACE_MATCH_WINDOW_DAYS = 1


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
    """add_version/upsert_draft_weeks input for one plan_weeks row.
    target_miles is the range floor (point week = lo only, no hi); omitting
    both target_miles and target_miles_hi is a deliberately unspecified week,
    scored on workout count alone. target_miles_hi/target_long_run_minutes/
    target_strength_days are optional range/duration/strength targets
    alongside the mileage ones."""
    week_start: str
    target_workouts: int
    phase: str
    target_miles: NotRequired[float | None]
    target_miles_hi: NotRequired[float | None]
    target_long_run_miles: NotRequired[float | None]
    target_long_run_minutes: NotRequired[float | None]
    target_strength_days: NotRequired[int | None]
    note: NotRequired[str | None]


class DayInput(TypedDict):
    """add_version/upsert_draft_days input for one plan_days row. target is
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
    """A full version snapshot: the version row plus every week/day row."""
    version: PlanVersionRow
    weeks: list[PlanWeekRow]
    days: list[PlanDayRow]


class DraftBundle(TypedDict):
    """Current state of a plan's one mutable draft, plus a gap report: plain-
    English messages naming what commit_plan's global validation would
    reject right now (unauthored weeks, days with no week row, not yet
    reaching the race week) — see get_draft."""
    plan: PlanRow
    version: PlanVersionRow
    weeks: list[PlanWeekRow]
    days: list[PlanDayRow]
    gaps: list[str]


class WeekDiff(TypedDict):
    week_start: str
    change: Literal["added", "removed", "changed"]
    changed_fields: list[str]  # populated only when change == "changed"


class DayDiff(TypedDict):
    date: str
    seq: int
    change: Literal["added", "removed", "changed"]
    changed_fields: list[str]  # populated only when change == "changed"


class VersionDiff(TypedDict):
    version_a: int
    version_b: int
    changed_weeks: list[WeekDiff]
    changed_days: list[DayDiff]


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
        "target_strength_days": (
            int(row["target_strength_days"]) if row["target_strength_days"] is not None else None
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


def _get_plan(conn: sqlite3.Connection, plan_id: int) -> PlanRow | None:
    row = conn.execute(
        "SELECT plan_id, title, race_date, distance_bucket, goal_time_s, status, created_at "
        "FROM plans WHERE plan_id = ?",
        [plan_id],
    ).fetchone()
    return _plan_row(row) if row is not None else None


# --- accessors ---------------------------------------------------------------

def get_active_plan(conn: sqlite3.Connection) -> PlanRow | None:
    """The one active plan, or None. (One active plan at a time is enforced by
    create_plan, so at most one row can ever have status='active'.)"""
    row = conn.execute(
        "SELECT plan_id, title, race_date, distance_bucket, goal_time_s, status, created_at "
        "FROM plans WHERE status = 'active' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return _plan_row(row) if row is not None else None


def get_version(conn: sqlite3.Connection, version_id: int) -> PlanVersionBundle | None:
    """A full version snapshot (version row + its weeks + its days), or None."""
    vrow = conn.execute(
        "SELECT version_id, plan_id, version_n, created_at, committed_at, note, author "
        "FROM plan_versions WHERE version_id = ?",
        [version_id],
    ).fetchone()
    if vrow is None:
        return None
    weeks = [
        _week_row(r) for r in conn.execute(
            "SELECT version_id, week_start, target_miles, target_miles_hi, target_workouts, "
            "target_long_run_miles, target_long_run_minutes, target_strength_days, phase, note "
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
    return {"version": _version_row(vrow), "weeks": weeks, "days": days}


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
            "SELECT version_id, plan_id, version_n, created_at, committed_at, note, author "
            "FROM plan_versions WHERE plan_id = ? AND committed_at IS NOT NULL ORDER BY version_n",
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

def get_most_recent_completed_plan(conn: sqlite3.Connection) -> PlanRow | None:
    """The most recently raced completed plan (by race_date), or None."""
    row = conn.execute(
        "SELECT plan_id, title, race_date, distance_bucket, goal_time_s, status, created_at "
        "FROM plans WHERE status = 'completed' ORDER BY race_date DESC LIMIT 1"
    ).fetchone()
    return _plan_row(row) if row is not None else None


def get_current_or_recent_plan(conn: sqlite3.Connection) -> PlanRow | None:
    """The active plan, or — when there is none — the most recently raced
    completed plan. Lets a consumer that only cares about "the one plan the
    athlete is looking at right now" (plan.html, via plan_api.py) keep
    serving a plan's retrospective the moment it auto-completes (see
    auto_complete_plan below), without a gap where it goes invisible.
    Abandoned plans never surface here; once more than one completed plan
    exists, a plans index page is the answer, not this fallback."""
    active = get_active_plan(conn)
    return active if active is not None else get_most_recent_completed_plan(conn)


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


def auto_complete_plan(conn: sqlite3.Connection) -> int | None:
    """Flips the active plan's status to 'completed' when a synced
    effective-race activity now matches its race_date/distance_bucket (see
    find_completing_race) — called from sync.py after each sync's upserts.
    No-ops cleanly and returns None: no active plan, or no matching race yet
    (this also covers "already completed", implicitly — only status='active'
    plans are ever considered, so a completed plan is simply invisible to the
    next call). The status flip is the only mutation — plans/plan_versions/
    plan_weeks/plan_days are untouched and stay append-only. Returns the
    completed plan_id, or None when nothing changed."""
    active = get_active_plan(conn)
    if active is None:
        return None
    match = find_completing_race(conn, active)
    if match is None:
        return None
    conn.execute("UPDATE plans SET status = 'completed' WHERE plan_id = ?", [active["plan_id"]])
    conn.commit()
    return active["plan_id"]


def completed_plans_by_race_date(conn: sqlite3.Connection) -> dict[str, PlanRow]:
    """Every completed plan, keyed by the exact synced date of its matching
    race (find_completing_race) — the join surface builds.html/races.html use
    to cross-link a race/build row to its plan's retrospective (both already
    key their own rows by that same activity date). A completed plan whose
    race was never synced, or no longer matches, is simply absent."""
    rows = conn.execute(
        "SELECT plan_id, title, race_date, distance_bucket, goal_time_s, status, created_at "
        "FROM plans WHERE status = 'completed'"
    ).fetchall()
    out: dict[str, PlanRow] = {}
    for row in rows:
        plan_row = _plan_row(row)
        match = find_completing_race(conn, plan_row)
        if match is not None:
            out[match["date"]] = plan_row
    return out


def create_plan(
    conn: sqlite3.Connection,
    *,
    title: str,
    race_date: str,
    distance_bucket: str,
    goal_time_s: int | None = None,
) -> int:
    """Creates a new active plan. Rejects a second active plan (v1 constraint —
    abandon/complete the existing one first)."""
    if not title.strip():
        raise PlanValidationError("plan title must not be empty")
    try:
        date.fromisoformat(race_date)
    except ValueError as e:
        raise PlanValidationError(f"race_date {race_date!r} is not a valid ISO date") from e
    if not distance_bucket.strip():
        raise PlanValidationError("distance_bucket must not be empty")
    if goal_time_s is not None and goal_time_s < 0:
        raise PlanValidationError(f"goal_time_s must be non-negative, got {goal_time_s}")

    existing = conn.execute("SELECT plan_id, title FROM plans WHERE status = 'active'").fetchone()
    if existing is not None:
        raise PlanValidationError(
            f"an active plan already exists (plan_id={int(existing['plan_id'])}, "
            f"title={existing['title']!r}); abandon or complete it before creating a new one"
        )

    created_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO plans (title, race_date, distance_bucket, goal_time_s, status, created_at) "
        "VALUES (?, ?, ?, ?, 'active', ?)",
        (title, race_date, distance_bucket, goal_time_s, created_at),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


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
    """Field-level checks for one week input, shared by add_version and
    upsert_draft_weeks. Structural checks that need the whole set (Mondays
    relative to each other, contiguity, ending at the race week) are each
    caller's job — a single week never has enough context for those."""
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
    strength_days = w.get("target_strength_days")
    if strength_days is not None and strength_days < 0:
        raise PlanValidationError(f"week {w['week_start']}: target_strength_days must be non-negative")
    if w["phase"] not in PHASES:
        raise PlanValidationError(f"week {w['week_start']}: phase {w['phase']!r} must be one of {PHASES}")


def _validate_day_fields(d: DayInput) -> None:
    """Field-level checks for one day input (excluding target, validated
    separately by _validate_day_target/_freeze_day_target), shared by
    add_version and upsert_draft_days."""
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


def add_version(
    conn: sqlite3.Connection,
    plan_id: int,
    *,
    weeks: list[WeekInput],
    days: list[DayInput],
    note: str | None,
    author: Literal["agent", "manual"],
    created_at: datetime | None = None,
) -> int:
    """Appends a new immutable version (version_n = prior max + 1) — there is
    no update path. Validates weeks (Mondays, contiguous, ending at the race
    week, non-negative targets, known phase) and days (known slot, non-negative
    target_miles, date falls within one of the given weeks), then freezes each
    day's target via _freeze_day_target. created_at defaults to now and is also
    the as_of date for resolving zone-anchored targets."""
    plan = _get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    if not weeks:
        raise PlanValidationError("a plan version must include at least one week")

    created_dt = created_at if created_at is not None else datetime.now(timezone.utc)
    creation_date = created_dt.date()

    sorted_weeks = sorted(weeks, key=lambda w: w["week_start"])
    parsed_weeks: list[tuple[date, WeekInput]] = []
    for w in sorted_weeks:
        try:
            wd = date.fromisoformat(w["week_start"])
        except ValueError as e:
            raise PlanValidationError(f"week_start {w['week_start']!r} is not a valid ISO date") from e
        if wd.weekday() != 0:
            raise PlanValidationError(f"week {w['week_start']} is not a Monday")
        _validate_week_fields(w)
        parsed_weeks.append((wd, w))

    for (prev_d, prev_w), (cur_d, cur_w) in zip(parsed_weeks, parsed_weeks[1:]):
        gap = (cur_d - prev_d).days
        if gap != 7:
            raise PlanValidationError(
                f"weeks are not contiguous: {prev_w['week_start']} to {cur_w['week_start']} "
                f"is {gap} days apart, expected 7"
            )

    race_dt = date.fromisoformat(plan["race_date"])
    race_monday = race_dt - timedelta(days=race_dt.weekday())
    last_week_date = parsed_weeks[-1][0]
    if last_week_date != race_monday:
        raise PlanValidationError(
            f"plan must end at the race week ({race_monday.isoformat()}); "
            f"last week given is {last_week_date.isoformat()}"
        )

    week_starts = {wd.isoformat() for wd, _ in parsed_weeks}

    frozen_days: list[tuple[DayInput, DayTarget | None]] = []
    for d in days:
        try:
            dd = date.fromisoformat(d["date"])
        except ValueError as e:
            raise PlanValidationError(f"day date {d['date']!r} is not a valid ISO date") from e
        _validate_day_fields(d)

        day_week_start = (dd - timedelta(days=dd.weekday())).isoformat()
        if day_week_start not in week_starts:
            raise PlanValidationError(
                f"day {d['date']} falls in week {day_week_start}, which is not among the given weeks"
            )

        target = d.get("target")
        frozen_target = _freeze_day_target(conn, d, target, creation_date) if target else None
        frozen_days.append((d, frozen_target))

    version_n_row = conn.execute(
        "SELECT COALESCE(MAX(version_n), 0) AS n FROM plan_versions WHERE plan_id = ?", [plan_id]
    ).fetchone()
    version_n = int(version_n_row["n"]) + 1

    # add_version has no draft concept — every version it creates is
    # committed immediately (committed_at = created_at), matching v1
    # semantics exactly for callers that still use this entry point.
    cur = conn.execute(
        "INSERT INTO plan_versions (plan_id, version_n, created_at, committed_at, note, author) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, version_n, created_dt.isoformat(), created_dt.isoformat(), note, author),
    )
    assert cur.lastrowid is not None
    version_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, "
        "target_workouts, target_long_run_miles, target_long_run_minutes, target_strength_days, "
        "phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                version_id, wd.isoformat(), w.get("target_miles"), w.get("target_miles_hi"),
                w["target_workouts"], w.get("target_long_run_miles"), w.get("target_long_run_minutes"),
                w.get("target_strength_days"), w["phase"], w.get("note"),
            )
            for wd, w in parsed_weeks
        ],
    )
    conn.executemany(
        "INSERT INTO plan_days (version_id, date, seq, slot, title, target_miles, target_json, "
        "terrain, note, target_minutes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                version_id, d["date"], d.get("seq", 1), d["slot"], d.get("title"),
                d.get("target_miles"), json.dumps(frozen_target) if frozen_target else None,
                d.get("terrain"), d.get("note"), d.get("target_minutes"),
            )
            for d, frozen_target in frozen_days
        ],
    )
    conn.commit()
    return version_id


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
    if _get_plan(conn, plan_id) is None:
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
# runs the same global validation add_version always ran (Mondays, contiguous,
# ending at the race week), re-freezes zone-anchored day targets as of the
# commit date, stamps committed_at on the SAME version_id, and the version
# becomes immutable — no new version_id is allocated at commit, only at draft
# creation (start_plan_draft / start_revision_draft).


def _require_draft_version(conn: sqlite3.Connection, version_id: int) -> int:
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


def _plan_start_monday(conn: sqlite3.Connection, plan_id: int) -> date | None:
    """The plan's first week (from version 1, which always governs from the
    plan's start regardless of its own committed_at), or None if v1 has no
    weeks yet."""
    row = conn.execute("""
        SELECT MIN(pw.week_start) AS d
        FROM plan_weeks pw JOIN plan_versions pv ON pv.version_id = pw.version_id
        WHERE pv.plan_id = ? AND pv.version_n = 1
    """, [plan_id]).fetchone()
    return date.fromisoformat(row["d"]) if row is not None and row["d"] is not None else None


def start_plan_draft(
    conn: sqlite3.Connection,
    *,
    title: str,
    race_date: str,
    distance_bucket: str,
    goal_time_s: int | None = None,
) -> tuple[int, int]:
    """Creates a new plan, status='draft', plus its empty first (draft)
    version. Unlike create_plan, does NOT reject a second active plan — a
    draft plan may coexist with an active one; commit_plan is where that
    conflict is actually rejected. Returns (plan_id, version_id)."""
    if not title.strip():
        raise PlanValidationError("plan title must not be empty")
    try:
        date.fromisoformat(race_date)
    except ValueError as e:
        raise PlanValidationError(f"race_date {race_date!r} is not a valid ISO date") from e
    if not distance_bucket.strip():
        raise PlanValidationError("distance_bucket must not be empty")
    if goal_time_s is not None and goal_time_s < 0:
        raise PlanValidationError(f"goal_time_s must be non-negative, got {goal_time_s}")

    created_at = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO plans (title, race_date, distance_bucket, goal_time_s, status, created_at) "
        "VALUES (?, ?, ?, ?, 'draft', ?)",
        (title, race_date, distance_bucket, goal_time_s, created_at),
    )
    assert cur.lastrowid is not None
    plan_id = int(cur.lastrowid)

    vcur = conn.execute(
        "INSERT INTO plan_versions (plan_id, version_n, created_at, committed_at, note, author) "
        "VALUES (?, 1, ?, NULL, NULL, 'agent')",
        (plan_id, created_at),
    )
    assert vcur.lastrowid is not None
    conn.commit()
    return plan_id, int(vcur.lastrowid)


def start_revision_draft(conn: sqlite3.Connection, plan_id: int) -> int:
    """Seeds a new draft version for plan_id by copying the current (latest
    committed) version's weeks/days verbatim — the athlete then edits
    incrementally via upsert_draft_weeks/upsert_draft_days. Rejects a plan
    with no committed version yet (nothing to revise — use start_plan_draft)
    or one that already has a draft in progress (one draft per plan).
    Returns the new draft's version_id."""
    if _get_plan(conn, plan_id) is None:
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

    version_n_row = conn.execute(
        "SELECT COALESCE(MAX(version_n), 0) AS n FROM plan_versions WHERE plan_id = ?", [plan_id]
    ).fetchone()
    version_n = int(version_n_row["n"]) + 1
    created_at = datetime.now(timezone.utc).isoformat()
    vcur = conn.execute(
        "INSERT INTO plan_versions (plan_id, version_n, created_at, committed_at, note, author) "
        "VALUES (?, ?, ?, NULL, NULL, 'agent')",
        (plan_id, version_n, created_at),
    )
    assert vcur.lastrowid is not None
    version_id = int(vcur.lastrowid)

    conn.executemany(
        "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, "
        "target_workouts, target_long_run_miles, target_long_run_minutes, target_strength_days, "
        "phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (version_id, w["week_start"], w["target_miles"], w["target_miles_hi"], w["target_workouts"],
             w["target_long_run_miles"], w["target_long_run_minutes"], w["target_strength_days"],
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
    conn.commit()
    return version_id


def upsert_draft_weeks(conn: sqlite3.Connection, version_id: int, weeks: list[WeekInput]) -> None:
    """Inserts or updates (by week_start) any subset of a draft's weeks —
    call it in batches as the athlete approves each block. Local validation
    only (Monday-aligned, non-negative, known phase, hi >= lo); contiguity and
    reaching the race week are commit_plan's job, not this one's, since a
    partial batch is expected to have gaps mid-authoring."""
    _require_draft_version(conn, version_id)
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
        "target_workouts, target_long_run_miles, target_long_run_minutes, target_strength_days, "
        "phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(version_id, week_start) DO UPDATE SET "
        "target_miles = excluded.target_miles, target_miles_hi = excluded.target_miles_hi, "
        "target_workouts = excluded.target_workouts, "
        "target_long_run_miles = excluded.target_long_run_miles, "
        "target_long_run_minutes = excluded.target_long_run_minutes, "
        "target_strength_days = excluded.target_strength_days, "
        "phase = excluded.phase, note = excluded.note",
        [
            (version_id, wd.isoformat(), w.get("target_miles"), w.get("target_miles_hi"), w["target_workouts"],
             w.get("target_long_run_miles"), w.get("target_long_run_minutes"), w.get("target_strength_days"),
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
    _require_draft_version(conn, version_id)
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
    _require_draft_version(conn, version_id)
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
    _require_draft_version(conn, version_id)
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


def _draft_gap_report(plan: PlanRow, weeks: list[PlanWeekRow], days: list[PlanDayRow]) -> list[str]:
    """Plain-English messages naming what commit_plan's global validation
    would reject right now: unauthored weeks (by ordinal week number from the
    earliest authored week), days whose week has no week row, and not yet
    reaching the race week. Never raises — this is a report, not a gate."""
    if not weeks:
        return ["no weeks authored yet"]

    week_starts = sorted(w["week_start"] for w in weeks)
    plan_start = date.fromisoformat(week_starts[0])
    race_dt = date.fromisoformat(plan["race_date"])
    race_monday = race_dt - timedelta(days=race_dt.weekday())
    last_authored = date.fromisoformat(week_starts[-1])

    have = {date.fromisoformat(w) for w in week_starts}
    scan_end = max(last_authored, race_monday)
    missing_idx: list[int] = []
    idx = 0
    cursor = plan_start
    while cursor <= scan_end:
        idx += 1
        if cursor not in have:
            missing_idx.append(idx)
        cursor += timedelta(weeks=1)

    gaps: list[str] = []
    for start_i, end_i in _compress_ranges(missing_idx):
        gaps.append(f"week {start_i} unauthored" if start_i == end_i else f"weeks {start_i}-{end_i} unauthored")

    if last_authored < race_monday:
        gaps.append(
            f"weeks don't reach race week ({race_monday.isoformat()}); "
            f"last authored week is {week_starts[-1]}"
        )

    week_start_set = set(week_starts)
    day_weeks = sorted({
        (date.fromisoformat(d["date"]) - timedelta(days=date.fromisoformat(d["date"]).weekday())).isoformat()
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
    plan = _get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    draft = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if draft is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")

    bundle = get_version(conn, int(draft["version_id"]))
    assert bundle is not None
    gaps = _draft_gap_report(plan, bundle["weeks"], bundle["days"])
    return {"plan": plan, "version": bundle["version"], "weeks": bundle["weeks"], "days": bundle["days"], "gaps": gaps}


def _snapshot_past_weeks(conn: sqlite3.Connection, plan_id: int, version_id: int) -> list[str]:
    """Past-week protection for a revision draft, ported from the legacy
    revise_training_plan tool so it has one home: any week whose Monday is on
    or before the start of the current week — already governing or already in
    progress — is overwritten in place with whatever version actually
    governed it at the time, regardless of what the draft proposed for that
    week/its days. Makes rewriting history to look adherent structurally
    impossible no matter which write path produced the draft. Returns the
    week_starts that were overridden this way."""
    this_monday = date.today() - timedelta(days=date.today().weekday())
    plan_start = _plan_start_monday(conn, plan_id)
    if plan_start is None:
        return []

    preserved: list[str] = []
    m = plan_start
    while m <= this_monday:
        governing = current_version_for_week(conn, plan_id, m)
        if governing is not None:
            iso = m.isoformat()
            week_row = next((w for w in governing["weeks"] if w["week_start"] == iso), None)
            week_end = (m + timedelta(days=6)).isoformat()
            conn.execute(
                "DELETE FROM plan_days WHERE version_id = ? AND date >= ? AND date <= ?",
                (version_id, iso, week_end),
            )
            if week_row is not None:
                conn.execute(
                    "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_miles_hi, "
                    "target_workouts, target_long_run_miles, target_long_run_minutes, "
                    "target_strength_days, phase, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(version_id, week_start) DO UPDATE SET "
                    "target_miles = excluded.target_miles, target_miles_hi = excluded.target_miles_hi, "
                    "target_workouts = excluded.target_workouts, "
                    "target_long_run_miles = excluded.target_long_run_miles, "
                    "target_long_run_minutes = excluded.target_long_run_minutes, "
                    "target_strength_days = excluded.target_strength_days, "
                    "phase = excluded.phase, note = excluded.note",
                    (version_id, iso, week_row["target_miles"], week_row["target_miles_hi"],
                     week_row["target_workouts"], week_row["target_long_run_miles"],
                     week_row["target_long_run_minutes"], week_row["target_strength_days"],
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
    guaranteed by upsert; contiguous, ends at the race week, every day falls
    in a committed week), snapshots past weeks for a revision (see
    _snapshot_past_weeks — skipped for a plan's first-ever commit, since
    backdated week 1 is legitimately authored history, not something to
    protect from itself), re-freezes every zone-anchored day target as of
    today (freeze-at-commit — commit IS "authoring" for that rule), stamps
    committed_at on the draft's own version_id (no new version_id is
    allocated), and flips the plan to status='active'.

    Rejects committing a brand-new plan (status='draft') while a different
    plan is already active — a draft plan may coexist with one, but only one
    can hold the active slot. Revising the currently-active plan itself is
    always allowed.
    """
    if not note or not note.strip():
        raise PlanValidationError("commit_plan requires a non-empty note")
    plan = _get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")

    draft = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if draft is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")
    version_id = int(draft["version_id"])

    if plan["status"] == "draft":
        other_active = conn.execute(
            "SELECT plan_id, title FROM plans WHERE status = 'active' AND plan_id != ?", [plan_id]
        ).fetchone()
        if other_active is not None:
            raise PlanValidationError(
                f"an active plan already exists (plan_id={int(other_active['plan_id'])}, "
                f"title={other_active['title']!r}); abandon or complete it before committing a new plan"
            )

    has_prior_committed = conn.execute(
        "SELECT 1 FROM plan_versions WHERE plan_id = ? AND committed_at IS NOT NULL LIMIT 1", [plan_id]
    ).fetchone() is not None
    if has_prior_committed:
        _snapshot_past_weeks(conn, plan_id, version_id)

    weeks = [_week_row(r) for r in conn.execute(
        "SELECT version_id, week_start, target_miles, target_miles_hi, target_workouts, "
        "target_long_run_miles, target_long_run_minutes, target_strength_days, phase, note "
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

    race_dt = date.fromisoformat(plan["race_date"])
    race_monday = race_dt - timedelta(days=race_dt.weekday())
    last_week = date.fromisoformat(weeks[-1]["week_start"])
    if last_week != race_monday:
        raise PlanValidationError(
            f"plan must end at the race week ({race_monday.isoformat()}); "
            f"last week given is {last_week.isoformat()}"
        )

    week_starts = {w["week_start"] for w in weeks}
    days = [_day_row(r) for r in conn.execute(
        "SELECT version_id, date, seq, slot, title, target_miles, target_json, terrain, note, target_minutes "
        "FROM plan_days WHERE version_id = ? ORDER BY date, seq",
        [version_id],
    ).fetchall()]
    for d in days:
        dd = date.fromisoformat(d["date"])
        wk = (dd - timedelta(days=dd.weekday())).isoformat()
        if wk not in week_starts:
            raise PlanValidationError(
                f"day {d['date']} falls in week {wk}, which is not among the committed weeks"
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
    plan = _get_plan(conn, plan_id)
    if plan is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    draft = conn.execute(
        "SELECT version_id FROM plan_versions WHERE plan_id = ? AND committed_at IS NULL", [plan_id]
    ).fetchone()
    if draft is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")
    version_id = int(draft["version_id"])

    conn.execute("DELETE FROM plan_days WHERE version_id = ?", [version_id])
    conn.execute("DELETE FROM plan_weeks WHERE version_id = ?", [version_id])
    conn.execute("DELETE FROM plan_versions WHERE version_id = ?", [version_id])
    if plan["status"] == "draft":
        conn.execute("DELETE FROM plans WHERE plan_id = ?", [plan_id])
    conn.commit()


def diff_versions(conn: sqlite3.Connection, version_a: int, version_b: int) -> VersionDiff:
    """Computed, never stored: changed weeks (which target fields changed) and
    added/removed/changed days between two versions of the same plan."""
    a = get_version(conn, version_a)
    b = get_version(conn, version_b)
    if a is None:
        raise PlanValidationError(f"version {version_a} does not exist")
    if b is None:
        raise PlanValidationError(f"version {version_b} does not exist")

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

    return {
        "version_a": version_a,
        "version_b": version_b,
        "changed_weeks": changed_weeks,
        "changed_days": changed_days,
    }
