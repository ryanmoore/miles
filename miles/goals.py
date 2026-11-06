"""Per-cycle training goals: what a cycle emphasises, stored on the plan version
and judged on request (adr/0007). A goal measures one number per plan week
(`GOAL_TYPES`) and compares it, always as >=, with a target: a weekly goal week
by week, a cycle goal rolled up over the cycle or a span of it. Weeks the sync
has not fully covered get no value, never zero."""

import json
import sqlite3
import statistics
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, NamedTuple, cast

from typing_extensions import NotRequired, TypedDict

from . import elements, notes, plan
from .db import PlanGoalRow
from .fitness import MILE_M
from .periods import monday_of
from .plan import PlanValidationError, require_draft_version
from .plan_adherence import flag_runs, spell, sync_cutoff
from .races import run_sport_filter

HORIZONS = ("weekly", "cycle")
AGGREGATES = ("weeks_at_least", "average")
_GOAL_FIELDS = {
    "key", "horizon", "type", "params", "target", "why", "overrides", "flag",
    "aggregate", "threshold", "from_week", "to_week",
}


class _Run(NamedTuple):
    tags: list[str]
    kind: str | None
    elements: list[Mapping[str, Any]]


@dataclass
class _Week:
    miles: float = 0.0
    runs: list[_Run] = field(default_factory=list)
    other: list[tuple[str, str]] = field(default_factory=list)  # (date, sport_type) of non-run activities


def _names(value: object, allowed: Collection[str] | None = None) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise PlanValidationError("expected a non-empty list of strings")
    if allowed is not None and (bad := [v for v in value if v not in allowed]):
        raise PlanValidationError(f"{bad} must each be one of {sorted(allowed)}")
    return list(value)


def _slugs(value: object) -> list[str]:
    return notes.normalize_tags(_names(value))


def _slug(value: object) -> str:
    if not isinstance(value, str) or not (slugs := notes.normalize_tags([value])):
        raise PlanValidationError("expected a tag")
    return slugs[0]


def _element_types(value: object) -> list[str]:
    return _names(value, notes.ELEMENT_TYPES)


def _weekly_miles(week: _Week, params: Mapping[str, Any]) -> float:
    return week.miles


def _element_sessions(week: _Week, params: Mapping[str, Any]) -> float:
    types = params["types"]
    return sum(any(e["type"] in types and e["count"] > 0 for e in r.elements) for r in week.runs)


def _tagged_runs(week: _Week, params: Mapping[str, Any]) -> float:
    return sum(params["tag"] in r.tags for r in week.runs)


def _run_kind(week: _Week, params: Mapping[str, Any]) -> float:
    return sum(r.kind in params["kinds"] for r in week.runs)


def _activity_days(week: _Week, params: Mapping[str, Any]) -> float:
    return len({d for d, sport in week.other if sport in params["sport_types"]})


# Each type: (week -> number, {param name: normalizer}). A new type is one function and one entry.
GOAL_TYPES: dict[str, tuple[Callable[[_Week, Mapping[str, object]], float], dict[str, Callable[[object], object]]]] = {
    "weekly_miles": (_weekly_miles, {}),
    "element_sessions": (_element_sessions, {"types": _element_types}),
    "tagged_runs": (_tagged_runs, {"tag": _slug}),
    "run_kind": (_run_kind, {"kinds": _slugs}),
    "activity_days": (_activity_days, {"sport_types": _names}),
}


# --- authoring ---------------------------------------------------------------


def _number(label: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PlanValidationError(f"{label} must be a number >= 0")
    return float(value)


def _monday(label: str, value: object, week_starts: Collection[str]) -> str:
    try:
        ok = isinstance(value, str) and date.fromisoformat(value).weekday() == 0
    except ValueError:
        ok = False
    if not ok:
        raise PlanValidationError(f"{label} {value!r} is not a Monday (YYYY-MM-DD)")
    if week_starts and value not in week_starts:
        raise PlanValidationError(f"{label} {value} is outside the plan's weeks")
    return str(value)


def _normalize(raw: Mapping[str, object], week_starts: Collection[str]) -> PlanGoalRow:
    unknown = set(raw) - _GOAL_FIELDS
    if unknown:
        raise PlanValidationError(f"goal has unknown fields {sorted(unknown)}; allowed: {sorted(_GOAL_FIELDS)}")
    key, why = raw.get("key"), raw.get("why")
    if not isinstance(key, str) or not key.strip():
        raise PlanValidationError("every goal needs a short key")
    if not isinstance(why, str) or not why.strip():
        raise PlanValidationError(f"goal {key!r}: why is required (one line of reasoning)")
    horizon, kind = raw.get("horizon"), raw.get("type")
    if horizon not in HORIZONS:
        raise PlanValidationError(f"goal {key!r}: horizon must be one of {HORIZONS}")
    if not isinstance(kind, str) or kind not in GOAL_TYPES:
        raise PlanValidationError(f"goal {key!r}: type must be one of {sorted(GOAL_TYPES)}")
    spec = GOAL_TYPES[kind][1]
    params = raw.get("params") or {}
    if not isinstance(params, dict) or set(params) != set(spec):
        raise PlanValidationError(f"goal {key!r}: {kind} takes params {sorted(spec)}")
    try:
        clean = {name: spec[name](params[name]) for name in spec}
    except PlanValidationError as e:
        raise PlanValidationError(f"goal {key!r}: params: {e}") from e
    goal: PlanGoalRow = {
        "key": key.strip(), "horizon": str(horizon), "type": kind, "params": clean,
        "target": _number(f"goal {key!r}: target", raw.get("target")), "why": why.strip(),
        "overrides": {}, "flag": False, "aggregate": None, "threshold": None, "from_week": None, "to_week": None,
    }
    if horizon == "weekly":
        extra = [f for f in ("aggregate", "threshold", "from_week", "to_week") if raw.get(f) is not None]
        if extra:
            raise PlanValidationError(f"goal {key!r}: {extra} apply to cycle goals only")
        overrides = raw.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise PlanValidationError(f"goal {key!r}: overrides must map a Monday to a target")
        goal["overrides"] = {
            _monday(f"goal {key!r}: override week", w, week_starts): _number(f"goal {key!r}: override for {w}", t)
            for w, t in overrides.items()
        }
        goal["flag"] = bool(raw.get("flag"))
        return goal
    if raw.get("overrides") or raw.get("flag"):
        raise PlanValidationError(f"goal {key!r}: overrides and flag apply to weekly goals only")
    aggregate = raw.get("aggregate")
    if aggregate not in AGGREGATES:
        raise PlanValidationError(f"goal {key!r}: a cycle goal needs aggregate, one of {AGGREGATES}")
    goal["aggregate"] = str(aggregate)
    if (aggregate == "weeks_at_least") != (raw.get("threshold") is not None):
        raise PlanValidationError(f"goal {key!r}: threshold goes with weeks_at_least, and only with it")
    if raw.get("threshold") is not None:
        goal["threshold"] = _number(f"goal {key!r}: threshold", raw["threshold"])
    for name in ("from_week", "to_week"):
        if raw.get(name) is not None:
            goal[name] = _monday(f"goal {key!r}: {name}", raw[name], week_starts)
    if goal["from_week"] and goal["to_week"] and goal["from_week"] > goal["to_week"]:
        raise PlanValidationError(f"goal {key!r}: from_week is after to_week")
    return goal


def validate_goals(goals: Sequence[Mapping[str, object]], week_starts: Collection[str] = ()) -> list[PlanGoalRow]:
    """Normalized goals, or PlanValidationError naming the goal and field. Dates
    are checked against `week_starts` when the plan has weeks."""
    out = [_normalize(g, week_starts) for g in goals]
    keys = [g["key"] for g in out]
    if len(set(keys)) != len(keys):
        raise PlanValidationError("goal keys must be unique")
    return out


def set_draft_goals(conn: sqlite3.Connection, version_id: int, goals: Sequence[Mapping[str, object]]) -> None:
    """Replaces the draft's goal list."""
    require_draft_version(conn, version_id)
    week_starts = [r[0] for r in conn.execute("SELECT week_start FROM plan_weeks WHERE version_id = ?", [version_id])]
    rows = validate_goals(goals, week_starts)
    conn.execute("DELETE FROM plan_goals WHERE version_id = ?", [version_id])
    conn.executemany(
        f"INSERT INTO plan_goals (version_id, {plan.GOAL_COLS}) VALUES (?{', ?' * 12})",
        [
            (version_id, g["key"], g["horizon"], g["type"], json.dumps(g["params"]), g["target"], g["why"],
             json.dumps(g["overrides"]), int(g["flag"]), g["aggregate"], g["threshold"], g["from_week"], g["to_week"])
            for g in rows
        ],
    )
    conn.commit()


# --- judging -----------------------------------------------------------------


class WeekResult(TypedDict):
    week_start: str
    target: float
    value: float | None  # None: the week is not scored for this goal
    met: bool | None


class Progress(TypedDict):
    so_far: float | None  # the count (weeks_at_least) or the mean (average)
    scored: int
    left: int
    met: bool
    reachable: bool | None  # weeks_at_least only


class GoalFlag(TypedDict):
    type: str
    goal: str
    weeks: int
    since: str
    message: str


class GoalResult(PlanGoalRow):
    weeks: NotRequired[list[WeekResult]]  # weekly goals
    flags: NotRequired[list[GoalFlag]]
    progress: NotRequired[Progress]  # cycle goals


class GoalResults(TypedDict):
    goals: list[GoalResult]


def _load_weeks(conn: sqlite3.Connection, goals: Sequence[PlanGoalRow], mondays: Sequence[str]) -> dict[str, _Week]:
    """What each scored week's goals read: run miles, notes and (when a goal
    needs them) stream detections, and the non-run activities."""
    weeks = {m: _Week() for m in mondays}
    if not weeks:
        return weeks
    first, last = mondays[0], (date.fromisoformat(mondays[-1]) + timedelta(days=6)).isoformat()
    clause, args = run_sport_filter()
    runs = conn.execute(
        f"SELECT activity_id, DATE(start_date) AS date, distance_m FROM activities "
        f"WHERE {clause} AND DATE(start_date) BETWEEN ? AND ?",
        [*args, first, last],
    ).fetchall()
    noted = {n["activity_id"]: n for n in notes.get_run_notes(conn, first, last)}
    detected = (
        elements.detect_for_activities(conn, [r["activity_id"] for r in runs])
        if any(g["type"] == "element_sessions" for g in goals) else {}
    )
    for r in runs:
        week = weeks.get(monday_of(date.fromisoformat(r["date"])).isoformat())
        if week is None:
            continue
        note = cast(dict[str, Any], noted.get(r["activity_id"], {}))
        week.miles += (r["distance_m"] or 0.0) / MILE_M
        week.runs.append(_Run(
            list(note.get("tags") or []), note.get("kind"),
            elements.merged(note.get("elements"), detected.get(r["activity_id"], ())),
        ))
    sports = sorted({s for g in goals if g["type"] == "activity_days" for s in g["params"]["sport_types"]})
    if sports:
        for r in conn.execute(
            f"SELECT DATE(start_date) AS date, sport_type FROM activities "
            f"WHERE sport_type IN ({','.join('?' * len(sports))}) AND DATE(start_date) BETWEEN ? AND ?",
            [*sports, first, last],
        ):
            week = weeks.get(monday_of(date.fromisoformat(r["date"])).isoformat())
            if week is not None:
                week.other.append((r["date"], r["sport_type"]))
    for week in weeks.values():
        week.miles = round(week.miles, 2)
    return weeks


def _value(g: PlanGoalRow, week: _Week) -> float:
    return GOAL_TYPES[g["type"]][0](week, g["params"])


def _weekly_result(
    g: PlanGoalRow, plan_weeks: Sequence[str], judged: Mapping[str, PlanGoalRow | None], data: Mapping[str, _Week]
) -> GoalResult:
    """`judged` holds, per scored week, the goal of that week's governing version."""
    rows: list[WeekResult] = []
    for w in plan_weeks:
        cur = judged.get(w)
        if cur is None:
            rows.append({"week_start": w, "target": g["overrides"].get(w, g["target"]), "value": None, "met": None})
            continue
        value, target = _value(cur, data[w]), cur["overrides"].get(w, cur["target"])
        rows.append({"week_start": w, "target": target, "value": round(value, 2), "met": value >= target})
    scored = [r for r in rows if r["value"] is not None]
    flags: list[GoalFlag] = []
    if g["flag"]:
        for start, end in flag_runs([not r["met"] for r in scored]):
            if end == len(scored) - 1:
                run = scored[start : end + 1]
                numbers = ", ".join(f"{r['value']:g} of {r['target']:g}" for r in run)
                flags.append({
                    "type": "goal_under_target", "goal": g["key"], "weeks": len(run), "since": run[0]["week_start"],
                    "message": f"{g['key']} {numbers} in the last {spell(len(run))} weeks",
                })
    return {**g, "weeks": rows, "flags": flags}


def _cycle_result(g: PlanGoalRow, plan_weeks: Sequence[str], data: Mapping[str, _Week]) -> GoalResult:
    span = [w for w in plan_weeks if (g["from_week"] or "") <= w <= (g["to_week"] or "9999")]
    values = [_value(g, data[w]) for w in span if w in data]
    left = len(span) - len(values)
    if g["aggregate"] == "weeks_at_least":
        count = sum(v >= (g["threshold"] or 0) for v in values)
        so_far, met, reachable = float(count), count >= g["target"], count + left >= g["target"]
    else:
        so_far = round(statistics.fmean(values), 1) if values else None
        met, reachable = so_far is not None and so_far >= g["target"], None
    return {**g, "progress": {"so_far": so_far, "scored": len(values), "left": left, "met": met, "reachable": reachable}}


def plan_goal_results(conn: sqlite3.Connection, plan_id: int) -> GoalResults:
    """Every goal of the plan's latest version (its draft when never committed).
    A weekly goal's scored weeks are judged by the goal of the version that
    governed each week; a cycle goal is judged as the latest version states it,
    over the weeks the sync has fully covered."""
    latest = plan.view_bundle(conn, plan_id)
    if latest is None or not latest["goals"]:
        return {"goals": []}
    plan_weeks = [w["week_start"] for w in latest["weeks"]]
    cutoff = sync_cutoff(conn)
    scored = [w for w in plan_weeks if date.fromisoformat(w) + timedelta(days=6) < cutoff]
    governing = {w: plan.current_version_for_week(conn, plan_id, date.fromisoformat(w)) for w in scored}
    by_week = {w: {g["key"]: g for g in b["goals"] if g["horizon"] == "weekly"} for w, b in governing.items() if b}
    data = _load_weeks(conn, [*latest["goals"], *(g for gs in by_week.values() for g in gs.values())], scored)
    return {"goals": [
        _weekly_result(g, plan_weeks, {w: gs.get(g["key"]) for w, gs in by_week.items()}, data)
        if g["horizon"] == "weekly" else _cycle_result(g, plan_weeks, data)
        for g in latest["goals"]
    ]}
