"""Plan compare (adr/0010): two sides, each a plan version, a plan's draft or
the athlete's synced runs over a block of weeks, aligned week by week. Backs
the `compare_plans` MCP tool and `/api/actual-weeks`; plan.html aligns the
same way client-side over `/api/plan`."""

import sqlite3
from datetime import date, timedelta
from typing import Mapping, cast
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
NEGLIGIBLE_MILES = 0.5  # a day's miles closer than this are the same; plan.html's CMP_NEGLIGIBLE_MI matches
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


def align(a: Side, b: Side) -> list[tuple[str, SideWeek | None, SideWeek | None]]:
    """Weeks paired by date when the sides' ranges overlap (a week only one side
    has pairs with None); otherwise ordinally, labelled Week 1, 2, ..."""
    wa = {w["week_start"]: w for w in a["weeks"]}
    wb = {w["week_start"]: w for w in b["weeks"]}
    if wa.keys() & wb.keys():
        return [(d, wa.get(d), wb.get(d)) for d in sorted(wa.keys() | wb.keys())]
    n = max(len(a["weeks"]), len(b["weeks"]))
    return [
        (f"Week {i + 1}", a["weeks"][i] if i < len(a["weeks"]) else None, b["weeks"][i] if i < len(b["weeks"]) else None)
        for i in range(n)
    ]


def _fmt_sessions(sessions: list[Session]) -> str:
    return "; ".join(
        " ".join(p for p in (slot, f"{miles:g} mi" if miles is not None else None, title) if p)
        for slot, miles, title in sessions
    ) or "rest"


def _differs(sa: list[Session], sb: list[Session], with_titles: bool) -> bool:
    if {s[0] for s in sa} != {s[0] for s in sb} or (with_titles and {s[2] or "" for s in sa} != {s[2] or "" for s in sb}):
        return True
    return abs(sum(s[1] or 0 for s in sa) - sum(s[1] or 0 for s in sb)) >= NEGLIGIBLE_MILES


def _day_diffs(a: SideWeek | None, b: SideWeek | None, with_titles: bool) -> list[dict[str, str]]:
    """Weekdays whose sessions differ (see _differs)."""
    out: list[dict[str, str]] = []
    for name in WEEKDAYS:
        sa = a["days"].get(name, []) if a else []
        sb = b["days"].get(name, []) if b else []
        if _differs(sa, sb, with_titles):
            out.append({"day": name, "a": _fmt_sessions(sa), "b": _fmt_sessions(sb)})
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


def _side_params(spec: Mapping[str, object], prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if spec.get("draft"):
        out[f"{prefix}draft"] = "1"
    if spec.get("from_week") and spec.get("to_week"):
        out[f"{prefix}weeks"] = f"{spec['from_week']}..{spec['to_week']}"
    return out


def compare_url(a: Mapping[str, object], b: Mapping[str, object]) -> str:
    query = {"plan": str(a["plan_id"]), **_side_params(a, ""), "vs": "actual" if b.get("actual") else str(b["plan_id"]),
             **_side_params(b, "vs_")}
    return app_url(f"/plan.html?{urlencode(query)}#plan")


def compare(conn: sqlite3.Connection, a: Mapping[str, object], b: Mapping[str, object]) -> dict[str, object]:
    """Both sides loaded, aligned, and compared. Side a must be a plan; b may be the athlete's actual runs."""
    if a.get("actual") or "plan_id" not in a:
        raise PlanValidationError("side a must be a plan (give plan_id); actual runs go in side b")
    if not b.get("actual") and "plan_id" not in b:
        raise PlanValidationError("side b needs plan_id, or actual=true with from_week and to_week")
    sa, sb = load_side(conn, a), load_side(conn, b)
    rows = align(sa, sb)
    with_titles = not sb["is_actual"]
    return {
        "url": compare_url(a, b),
        "a": _summary(sa), "b": _summary(sb),
        "weeks": [
            {
                "week": label,
                "a": {"week_start": wa["week_start"], "miles": wa["miles"], "workouts": wa["workouts"], "long_run_miles": wa["long_run_miles"]} if wa else None,
                "b": {"week_start": wb["week_start"], "miles": wb["miles"], "workouts": wb["workouts"], "long_run_miles": wb["long_run_miles"]} if wb else None,
                "day_differences": _day_diffs(wa, wb, with_titles),
            }
            for label, wa, wb in rows
        ],
    }
