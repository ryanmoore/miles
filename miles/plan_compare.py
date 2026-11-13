"""Plan compare (adr/0010): two to four sides, each a plan version, a plan's
draft or the athlete's synced runs over a block of weeks, aligned week by week
against the first (the baseline). Backs the `compare_plans` MCP tool and
`/api/actual-weeks`; plan.html aligns the same way client-side over
`/api/plan`."""

import sqlite3
from datetime import date, timedelta
from typing import Mapping, Sequence, cast
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from typing_extensions import TypedDict

from . import plan
from .db import PlanDayRow, PlanWeekRow
from .derive import connect_derived as _conn
from .format import app_url
from .plan import PlanValidationError
from .plan_api import PlanActivity, _activities_by_date, _actual_by_week

router = APIRouter()

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A day's miles within max(NEGLIGIBLE_MILES, NEGLIGIBLE_FRACTION of the larger day) are the same; plan.html's CMP_NEGLIGIBLE_* match.
NEGLIGIBLE_MILES = 1.0
NEGLIGIBLE_FRACTION = 0.15
RUN_TYPE_SLOT = {"long_run": "long", "workout": "workout", "race": "race"}


class ActualWeekOut(TypedDict):
    week_start: str
    actual_miles: float
    actual_workouts: int


class ActualWeeksResponse(TypedDict):
    weeks: list[ActualWeekOut]
    actual: dict[str, list[PlanActivity]]  # keyed by date, every synced run in the weeks


# One session: (slot, miles, title). Title is None for a synced run.
Session = tuple[str, float | None, str | None]


class SideWeek(TypedDict):
    week_start: str
    miles: float | None  # the weekly chart's number: a range week's midpoint
    workouts: int | None
    long_run_miles: float | None
    days: dict[str, list[Session]]  # weekday name -> sessions, rest days omitted


class Side(TypedDict):
    label: str
    is_actual: bool
    weeks: list[SideWeek]


def _monday(value: str, name: str) -> date:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise PlanValidationError(f"{name} {value!r} is not an ISO date") from None
    if d.weekday() != 0:
        raise PlanValidationError(f"{name} {value} is not a Monday")
    return d


def _range(frm: str | None, to: str | None) -> tuple[date, date] | None:
    if frm is None and to is None:
        return None
    if frm is None or to is None:
        raise PlanValidationError("from_week and to_week go together")
    lo, hi = _monday(frm, "from_week"), _monday(to, "to_week")
    if hi < lo:
        raise PlanValidationError("to_week is before from_week")
    return lo, hi


def _spec_range(spec: Mapping[str, object]) -> tuple[date, date] | None:
    return _range(cast("str | None", spec.get("from_week")), cast("str | None", spec.get("to_week")))


def actual_weeks(conn: sqlite3.Connection, lo: date, hi: date) -> ActualWeeksResponse:
    end = (hi + timedelta(days=6)).isoformat()
    by_week = _actual_by_week(conn, lo.isoformat(), end)
    weeks: list[ActualWeekOut] = []
    for i in range((hi - lo).days // 7 + 1):
        monday = (lo + timedelta(weeks=i)).isoformat()
        miles, workouts = by_week.get(monday, (0.0, 0))
        weeks.append({"week_start": monday, "actual_miles": miles, "actual_workouts": workouts})
    return {"weeks": weeks, "actual": _activities_by_date(conn, lo.isoformat(), end)}


@router.get("/api/actual-weeks")
def get_actual_weeks(from_week: str, to_week: str) -> ActualWeeksResponse:
    """The athlete's synced runs per Monday-aligned week and per date over
    from_week..to_week (both Mondays, inclusive)."""
    try:
        window = _range(from_week, to_week)
    except PlanValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    assert window is not None
    return actual_weeks(_conn(), *window)


def _rep_miles(lo: float | None, hi: float | None) -> float | None:
    return lo if hi is None or lo is None else (lo + hi) / 2


def _sessions(days: list[PlanDayRow], week_start: str) -> dict[str, list[Session]]:
    monday = date.fromisoformat(week_start)
    out: dict[str, list[Session]] = {}
    for d in days:
        offset = (date.fromisoformat(d["date"]) - monday).days
        if 0 <= offset < 7 and not (d["slot"] == "rest" and not d["title"] and not d["target_miles"]):
            out.setdefault(WEEKDAYS[offset], []).append((d["slot"], d["target_miles"], d["title"]))
    return out


def _plan_side(conn: sqlite3.Connection, spec: Mapping[str, object]) -> Side:
    plan_id = cast(int, spec["plan_id"])
    row = plan.get_plan(conn, plan_id)
    if row is None:
        raise PlanValidationError(f"plan {plan_id} does not exist")
    draft = bool(spec.get("draft"))
    bundle = plan.view_bundle(conn, plan_id, draft=draft)
    if bundle is None:
        raise PlanValidationError(f"plan {plan_id} has no draft in progress")
    window = _spec_range(spec)
    weeks: list[PlanWeekRow] = [
        w for w in bundle["weeks"]
        if window is None or window[0].isoformat() <= w["week_start"] <= window[1].isoformat()
    ]
    if not weeks:
        raise PlanValidationError(f"plan {plan_id} has no weeks in that range")
    return {
        "label": row["title"] + (" (draft)" if draft else ""),
        "is_actual": False,
        "weeks": [
            {
                "week_start": w["week_start"],
                "miles": _rep_miles(w["target_miles"], w["target_miles_hi"]),
                "workouts": w["target_workouts"],
                "long_run_miles": w["target_long_run_miles"],
                "days": _sessions(bundle["days"], w["week_start"]),
            }
            for w in weeks
        ],
    }


def _actual_side(conn: sqlite3.Connection, spec: Mapping[str, object]) -> Side:
    window = _spec_range(spec)
    if window is None:
        raise PlanValidationError("an actual side needs from_week and to_week")
    data = actual_weeks(conn, *window)
    weeks: list[SideWeek] = []
    for w in data["weeks"]:
        monday = date.fromisoformat(w["week_start"])
        runs = [(WEEKDAYS[i], r) for i in range(7) for r in data["actual"].get((monday + timedelta(days=i)).isoformat(), [])]
        days: dict[str, list[Session]] = {}
        for name, r in runs:
            days.setdefault(name, []).append((RUN_TYPE_SLOT.get(r["run_type"], "easy"), r["distance_mi"], None))
        weeks.append({
            "week_start": w["week_start"], "miles": w["actual_miles"], "workouts": w["actual_workouts"],
            "long_run_miles": max((r["distance_mi"] for _, r in runs), default=None), "days": days,
        })
    return {"label": "Actual runs", "is_actual": True, "weeks": weeks}


def load_side(conn: sqlite3.Connection, spec: Mapping[str, object]) -> Side:
    return _actual_side(conn, spec) if spec.get("actual") else _plan_side(conn, spec)


def align(sides: list[Side]) -> list[tuple[str, list[SideWeek | None]]]:
    """Weeks paired by date when every other side shares a week with the
    baseline (sides[0]); a week only some sides have pairs with None for the
    rest. Otherwise all sides pair by position, labelled Week 1, 2, ..."""
    by_date = [{w["week_start"]: w for w in s["weeks"]} for s in sides]
    if all(m.keys() & by_date[0].keys() for m in by_date[1:]):
        return [(d, [m.get(d) for m in by_date]) for d in sorted(set().union(*by_date))]
    n = max(len(s["weeks"]) for s in sides)
    return [(f"Week {i + 1}", [s["weeks"][i] if i < len(s["weeks"]) else None for s in sides]) for i in range(n)]


def _fmt_sessions(sessions: list[Session]) -> str:
    return "; ".join(
        " ".join(p for p in (slot, f"{miles:g} mi" if miles is not None else None, title) if p)
        for slot, miles, title in sessions
    ) or "rest"


def _differs(base: list[Session], other: list[Session], with_titles: bool) -> bool:
    """Session types (or, with_titles, titles) differ, or total miles differ by
    more than the negligible gap; plan.html's cmpLevel applies the same rule."""
    if {s[0] for s in base} != {s[0] for s in other} or (with_titles and {s[2] or "" for s in base} != {s[2] or "" for s in other}):
        return True
    a, b = sum(s[1] or 0 for s in base), sum(s[1] or 0 for s in other)
    return abs(a - b) > max(NEGLIGIBLE_MILES, NEGLIGIBLE_FRACTION * max(a, b))


def _day_diffs(base: SideWeek | None, other: SideWeek | None, with_titles: bool) -> list[dict[str, str]]:
    """Weekdays whose sessions differ from the baseline's (see _differs)."""
    out: list[dict[str, str]] = []
    for name in WEEKDAYS:
        sb = base["days"].get(name, []) if base else []
        so = other["days"].get(name, []) if other else []
        if _differs(sb, so, with_titles):
            out.append({"day": name, "baseline": _fmt_sessions(sb), "side": _fmt_sessions(so)})
    return out


def _summary(side: Side) -> dict[str, object]:
    miles = [w["miles"] for w in side["weeks"] if w["miles"] is not None]
    longs = [w["long_run_miles"] for w in side["weeks"] if w["long_run_miles"] is not None]
    workouts = [w["workouts"] for w in side["weeks"] if w["workouts"] is not None]
    return {
        "label": side["label"], "weeks": len(side["weeks"]),
        "total_miles": round(sum(miles), 1), "avg_week_miles": round(sum(miles) / len(miles), 1) if miles else None,
        "peak_week_miles": max(miles, default=None),
        "workouts_per_week": round(sum(workouts) / len(workouts), 1) if workouts else None,
        "longest_long_run": max(longs, default=None),
    }


def _vs_spec(spec: Mapping[str, object]) -> str:
    """A side's `vs=` token: `<plan id>|actual[:draft][:FROM..TO]`."""
    parts = ["actual" if spec.get("actual") else str(spec["plan_id"])]
    if spec.get("draft"):
        parts.append("draft")
    if spec.get("from_week") and spec.get("to_week"):
        parts.append(f"{spec['from_week']}..{spec['to_week']}")
    return ":".join(parts)


def compare_url(sides: Sequence[Mapping[str, object]]) -> str:
    base = sides[0]
    query = [("plan", str(base["plan_id"]))]
    if base.get("draft"):
        query.append(("draft", "1"))
    if base.get("from_week") and base.get("to_week"):
        query.append(("weeks", f"{base['from_week']}..{base['to_week']}"))
    query += [("vs", _vs_spec(s)) for s in sides[1:]]
    return app_url(f"/plan.html?{urlencode(query, safe=':')}#plan")


def compare(conn: sqlite3.Connection, specs: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """2-4 sides loaded, aligned, and compared; every side after the first is
    compared to the first (the baseline), never to each other."""
    if not 2 <= len(specs) <= 4:
        raise PlanValidationError("give 2 to 4 sides")
    if specs[0].get("actual") or "plan_id" not in specs[0]:
        raise PlanValidationError("the first side is the baseline and must be a plan (give plan_id)")
    if any(not s.get("actual") and "plan_id" not in s for s in specs):
        raise PlanValidationError("each side needs plan_id, or actual=true with from_week and to_week")
    sides = [load_side(conn, s) for s in specs]
    weeks: list[dict[str, object]] = []
    for label, cells in align(sides):
        entries: list[dict[str, object] | None] = []
        for i, w in enumerate(cells):
            if w is None:
                entries.append(None)
                continue
            entry: dict[str, object] = {k: w[k] for k in ("week_start", "miles", "workouts", "long_run_miles")}
            if i:
                entry["day_differences"] = _day_diffs(cells[0], w, not sides[i]["is_actual"])
            entries.append(entry)
        weeks.append({"week": label, "sides": entries})
    return {"url": compare_url(specs), "sides": [_summary(s) for s in sides], "weeks": weeks}
