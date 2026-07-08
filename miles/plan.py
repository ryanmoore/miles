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
SLOTS: tuple[str, ...] = ("easy", "workout", "long", "rest", "race")
LOG_ACTIONS: tuple[str, ...] = ("skipped", "moved", "modified", "note")
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
    zone_name is kept alongside the frozen paces for display."""
    reps: int
    rep_distance_m: float
    pace_lo: float
    pace_hi: float
    zone_name: str
    hr_lo: float
    hr_hi: float


class WeekInput(TypedDict):
    """add_version input for one plan_weeks row."""
    week_start: str
    target_miles: float
    target_workouts: int
    phase: str
    target_long_run_miles: NotRequired[float | None]
    note: NotRequired[str | None]


class DayInput(TypedDict):
    """add_version input for one plan_days row. target is resolved/frozen
    before storage; see _freeze_day_target."""
    date: str
    slot: str
    seq: NotRequired[int]
    title: NotRequired[str | None]
    target_miles: NotRequired[float | None]
    target: NotRequired[DayTarget | None]


class PlanVersionBundle(TypedDict):
    """A full version snapshot: the version row plus every week/day row."""
    version: PlanVersionRow
    weeks: list[PlanWeekRow]
    days: list[PlanDayRow]


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
        "note": row["note"],
        "author": row["author"],
    }


def _week_row(row: sqlite3.Row) -> PlanWeekRow:
    return {
        "version_id": int(row["version_id"]),
        "week_start": row["week_start"],
        "target_miles": float(row["target_miles"]),
        "target_workouts": int(row["target_workouts"]),
        "target_long_run_miles": (
            float(row["target_long_run_miles"]) if row["target_long_run_miles"] is not None else None
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
        "SELECT version_id, plan_id, version_n, created_at, note, author "
        "FROM plan_versions WHERE version_id = ?",
        [version_id],
    ).fetchone()
    if vrow is None:
        return None
    weeks = [
        _week_row(r) for r in conn.execute(
            "SELECT version_id, week_start, target_miles, target_workouts, "
            "target_long_run_miles, phase, note FROM plan_weeks "
            "WHERE version_id = ? ORDER BY week_start",
            [version_id],
        ).fetchall()
    ]
    days = [
        _day_row(r) for r in conn.execute(
            "SELECT version_id, date, seq, slot, title, target_miles, target_json "
            "FROM plan_days WHERE version_id = ? ORDER BY date, seq",
            [version_id],
        ).fetchall()
    ]
    return {"version": _version_row(vrow), "weeks": weeks, "days": days}


def current_version_for_week(
    conn: sqlite3.Connection, plan_id: int, week_start: date
) -> PlanVersionBundle | None:
    """The version that governs week_start: the latest version whose
    created_at is strictly before week_start, with a floor — version 1 governs
    from the plan's first week regardless of its own created_at. Returns None
    when week_start precedes the plan's first week, or the plan has no
    versions yet."""
    if week_start.weekday() != 0:
        raise PlanValidationError(f"week_start {week_start.isoformat()} is not a Monday")

    versions = [
        _version_row(r) for r in conn.execute(
            "SELECT version_id, plan_id, version_n, created_at, note, author "
            "FROM plan_versions WHERE plan_id = ? ORDER BY version_n",
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
    eligible = [v for v in versions if v["version_n"] == 1 or v["created_at"][:10] < week_iso]
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


def _freeze_day_target(
    conn: sqlite3.Connection, day: DayInput, target: DayTarget, creation_date: date
) -> DayTarget:
    """Validates and freezes one day's target. Explicit pace_lo/pace_hi are
    always allowed as-is. A zone_name with no explicit paces is resolved via a
    live estimate_fitness(as_of=creation_date) + zones_from_predicted: 'easy'
    freezes to the easy_range band, the other four zones freeze to their
    fitness.py anchor +/- that zone's tolerance. Rejects (never stores
    unresolved) when no fitness estimate is computable."""
    day_label = f"day {day['date']} ({day.get('title') or day['slot']})"
    frozen = cast(DayTarget, dict(target))

    reps = frozen.get("reps")
    if reps is not None and reps < 0:
        raise PlanValidationError(f"{day_label}: target reps must be non-negative")
    rep_distance_m = frozen.get("rep_distance_m")
    if rep_distance_m is not None and rep_distance_m < 0:
        raise PlanValidationError(f"{day_label}: target rep_distance_m must be non-negative")

    hr_lo = frozen.get("hr_lo")
    hr_hi = frozen.get("hr_hi")
    if hr_lo is not None and hr_lo < 0:
        raise PlanValidationError(f"{day_label}: target hr_lo must be non-negative")
    if hr_hi is not None and hr_hi < 0:
        raise PlanValidationError(f"{day_label}: target hr_hi must be non-negative")
    if hr_lo is not None and hr_hi is not None and hr_lo > hr_hi:
        raise PlanValidationError(f"{day_label}: target hr_lo must be <= hr_hi")

    zone_name = frozen.get("zone_name")
    if zone_name is not None and zone_name not in ZONE_NAMES:
        raise PlanValidationError(f"{day_label}: zone_name {zone_name!r} must be one of {ZONE_NAMES}")

    pace_lo = frozen.get("pace_lo")
    pace_hi = frozen.get("pace_hi")
    if pace_lo is not None or pace_hi is not None:
        if pace_lo is None or pace_hi is None:
            raise PlanValidationError(f"{day_label}: pace_lo and pace_hi must both be given together")
        if pace_lo < 0 or pace_hi < 0:
            raise PlanValidationError(f"{day_label}: pace_lo/pace_hi must be non-negative")
        if pace_lo > pace_hi:
            raise PlanValidationError(f"{day_label}: pace_lo must be <= pace_hi")
        return frozen  # explicit paces always allowed, no resolution needed

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
        if w["target_miles"] < 0:
            raise PlanValidationError(f"week {w['week_start']}: target_miles must be non-negative")
        if w["target_workouts"] < 0:
            raise PlanValidationError(f"week {w['week_start']}: target_workouts must be non-negative")
        long_run = w.get("target_long_run_miles")
        if long_run is not None and long_run < 0:
            raise PlanValidationError(
                f"week {w['week_start']}: target_long_run_miles must be non-negative"
            )
        if w["phase"] not in PHASES:
            raise PlanValidationError(f"week {w['week_start']}: phase {w['phase']!r} must be one of {PHASES}")
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
        if d["slot"] not in SLOTS:
            raise PlanValidationError(f"day {d['date']}: slot {d['slot']!r} must be one of {SLOTS}")
        seq = d.get("seq", 1)
        if seq < 1:
            raise PlanValidationError(f"day {d['date']}: seq must be >= 1")
        target_miles = d.get("target_miles")
        if target_miles is not None and target_miles < 0:
            raise PlanValidationError(f"day {d['date']}: target_miles must be non-negative")

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

    cur = conn.execute(
        "INSERT INTO plan_versions (plan_id, version_n, created_at, note, author) VALUES (?, ?, ?, ?, ?)",
        (plan_id, version_n, created_dt.isoformat(), note, author),
    )
    assert cur.lastrowid is not None
    version_id = int(cur.lastrowid)

    conn.executemany(
        "INSERT INTO plan_weeks (version_id, week_start, target_miles, target_workouts, "
        "target_long_run_miles, phase, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                version_id, wd.isoformat(), w["target_miles"], w["target_workouts"],
                w.get("target_long_run_miles"), w["phase"], w.get("note"),
            )
            for wd, w in parsed_weeks
        ],
    )
    conn.executemany(
        "INSERT INTO plan_days (version_id, date, seq, slot, title, target_miles, target_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                version_id, d["date"], d.get("seq", 1), d["slot"], d.get("title"),
                d.get("target_miles"), json.dumps(frozen_target) if frozen_target else None,
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
