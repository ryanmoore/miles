"""Supplementary sessions (adr/0011): the plan's non-run practice, each with a
checklist, and what the athlete did with them. The plan says what a session is;
`plan_log` events beside it say what happened (items ticked, a move to another
day, a skip). Events are append-only and the latest one per subject wins, so a
check can be unticked and a move undone, and nothing here bumps a version."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Literal, NotRequired, cast

from typing_extensions import TypedDict

from . import plan
from .db import STRENGTH_SPORT_TYPES, PlanDayRow
from .plan import DayTarget, PlanValidationError

STRENGTH_KIND = "strength"

State = Literal["done", "partial", "skipped", "open"]


class ItemState(TypedDict):
    name: str
    amount: NotRequired[str]
    checked: bool


class Session(TypedDict):
    date: str  # the planned date, with seq the session's identity
    seq: int
    kind: str
    title: str | None
    items: list[ItemState]
    marked_done: bool
    skipped: bool
    moved_to: str | None
    strength_activity: bool  # a strength activity was recorded on effective_date (kind strength only)
    effective_date: str  # where the session now lives: moved_to, else date
    state: State


class _Events:
    def __init__(self) -> None:
        self.checks: dict[str, bool] = {}
        self.done = False
        self.skipped = False
        self.moved_to: str | None = None


def _fold_events(conn: sqlite3.Connection, plan_id: int) -> dict[tuple[str, int], _Events]:
    out: dict[tuple[str, int], _Events] = {}
    for r in conn.execute(
        "SELECT date, seq, action, detail FROM plan_log WHERE plan_id = ? AND seq IS NOT NULL ORDER BY log_id",
        [plan_id],
    ):
        ev, detail = out.setdefault((r["date"], r["seq"]), _Events()), json.loads(r["detail"])
        match r["action"]:
            case "check":
                ev.checks[detail["item"]] = detail["on"]
            case "done":
                ev.done = detail["on"]
            case "skip":
                ev.skipped = detail["on"]
            case "move":
                ev.moved_to = detail["to"]
    return out


def _strength_dates(conn: sqlite3.Connection, start: str, end: str) -> set[str]:
    marks = ",".join("?" * len(STRENGTH_SPORT_TYPES))
    return {
        r[0] for r in conn.execute(
            f"SELECT DISTINCT DATE(start_date) FROM activities "
            f"WHERE sport_type IN ({marks}) AND DATE(start_date) BETWEEN ? AND ?",
            [*STRENGTH_SPORT_TYPES, start, end],
        )
    }


def session_state(
    ticked: int, total: int, *, marked_done: bool, skipped: bool, strength_activity: bool
) -> State:
    """Skipped wins. Done is every item ticked, an explicit done mark, or (for
    strength) a strength activity that day; partial is some items ticked."""
    if skipped:
        return "skipped"
    if marked_done or strength_activity or (total > 0 and ticked == total):
        return "done"
    return "partial" if ticked else "open"


def sessions(conn: sqlite3.Connection, plan_id: int, days: Sequence[PlanDayRow]) -> list[Session]:
    """The supplementary sessions among `days`, with the athlete's events folded
    in, ordered by effective date then planned date and seq."""
    planned = [d for d in days if d["slot"] == "supplementary"]
    events = _fold_events(conn, plan_id) if planned else {}
    effective = {
        (d["date"], d["seq"]): events.get((d["date"], d["seq"]), _Events()).moved_to or d["date"] for d in planned
    }
    strength_on: set[str] = set()
    if effective:
        strength_on = _strength_dates(conn, min(effective.values()), max(effective.values()))
    out: list[Session] = []
    for d in planned:
        target = cast(DayTarget, json.loads(d["target_json"] or "{}"))
        ev = events.get((d["date"], d["seq"]), _Events())
        items: list[ItemState] = [
            cast(ItemState, {**item, "checked": ev.checks.get(item["name"], False)}) for item in target.get("items", [])
        ]
        kind, eff = target.get("kind", ""), effective[(d["date"], d["seq"])]
        activity = kind == STRENGTH_KIND and eff in strength_on
        out.append({
            "date": d["date"], "seq": d["seq"], "kind": kind, "title": d["title"], "items": items,
            "marked_done": ev.done, "skipped": ev.skipped, "moved_to": ev.moved_to, "strength_activity": activity,
            "effective_date": eff,
            "state": session_state(
                sum(i["checked"] for i in items), len(items), marked_done=ev.done, skipped=ev.skipped,
                strength_activity=activity,
            ),
        })
    return sorted(out, key=lambda s: (s["effective_date"], s["date"], s["seq"]))


def plan_sessions(conn: sqlite3.Connection, plan_id: int) -> list[Session]:
    """Every supplementary session of the plan as it reads now."""
    bundle = plan.view_bundle(conn, plan_id)
    return sessions(conn, plan_id, bundle["days"] if bundle else [])


def record(
    conn: sqlite3.Connection,
    plan_id: int,
    session_date: str,
    seq: int,
    *,
    check: Sequence[str] = (),
    uncheck: Sequence[str] = (),
    done: bool | None = None,
    skip: bool | None = None,
    move_to: str | None = None,
) -> Session:
    """Appends events for one planned session and returns it as it now reads.
    move_to is any date; the planned date (or "") puts the session back. Raises
    PlanValidationError, writing nothing, for an unknown session or item."""
    session = next((s for s in plan_sessions(conn, plan_id) if (s["date"], s["seq"]) == (session_date, seq)), None)
    if session is None:
        raise PlanValidationError(f"plan {plan_id} has no supplementary session on {session_date} (seq {seq})")
    known = {i["name"] for i in session["items"]}
    if unknown := sorted((set(check) | set(uncheck)) - known):
        raise PlanValidationError(f"session {session_date} has no items named {unknown}; items: {sorted(known)}")
    if set(check) & set(uncheck):
        raise PlanValidationError("an item cannot be both checked and unchecked")
    events: list[tuple[str, dict[str, object]]] = [
        *(("check", {"item": n, "on": True}) for n in check),
        *(("check", {"item": n, "on": False}) for n in uncheck),
    ]
    if done is not None:
        events.append(("done", {"on": done}))
    if skip is not None:
        events.append(("skip", {"on": skip}))
    if move_to is not None:
        try:
            if move_to:
                date.fromisoformat(move_to)
        except ValueError as e:
            raise PlanValidationError(f"move_to {move_to!r} is not a valid ISO date") from e
        events.append(("move", {"to": move_to if move_to and move_to != session_date else None}))
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO plan_log (plan_id, date, action, created_at, seq, detail) VALUES (?, ?, ?, ?, ?, ?)",
        [(plan_id, session_date, action, now, seq, json.dumps(detail)) for action, detail in events],
    )
    conn.commit()
    return next(s for s in plan_sessions(conn, plan_id) if (s["date"], s["seq"]) == (session_date, seq))
