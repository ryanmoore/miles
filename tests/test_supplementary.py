from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from helpers import call_tool, insert_activity, memory_db

from miles import goals, mcp_server, plan, plan_api, supplementary
from miles.periods import monday_of

THIS_MONDAY = monday_of(date.today())


def _day(week: int, n: int) -> str:
    return (THIS_MONDAY + timedelta(weeks=week, days=n)).isoformat()


LIFT = [{"name": "Goblet squat", "amount": "3x8"}, {"name": "Plank", "amount": "2 min"}, {"name": "Jump rope", "amount": "x100"}]


def _plan(conn, *, items=LIFT, kind="strength", goal_list=()) -> int:
    """Weeks -3..1: a supplementary session on each week's Wednesday (the one in week -1 is `kind`/`items`)."""
    plan_id, version_id = plan.start_plan_draft(conn, title="Cycle")
    plan.upsert_draft_weeks(conn, version_id, [
        {"week_start": _day(w, 0), "target_workouts": 1, "phase": "base"} for w in range(-3, 2)
    ])
    plan.upsert_draft_days(conn, version_id, [
        {"date": _day(w, 2), "slot": "supplementary", "title": "Practice", "target": {"kind": kind, "items": items}}
        for w in range(-3, 2)
    ])
    goals.set_draft_goals(conn, version_id, list(goal_list))
    plan.commit_plan(conn, plan_id, note="ok")
    return plan_id


def _session(conn, plan_id: int, planned: str) -> supplementary.Session:
    return next(s for s in supplementary.plan_sessions(conn, plan_id) if s["date"] == planned)


@pytest.mark.parametrize(
    ("ticked", "total", "marked", "skipped", "strength", "state"),
    [
        (0, 3, False, False, False, "open"),
        (1, 3, False, False, False, "partial"),
        (3, 3, False, False, False, "done"),
        (0, 0, False, False, False, "open"),  # no items: only an explicit mark finishes it
        (0, 0, True, False, False, "done"),
        (0, 3, False, False, True, "done"),  # a strength activity that day
        (3, 3, False, True, False, "skipped"),  # skipped wins
        (1, 3, True, False, False, "done"),
    ],
)
def test_session_state(ticked, total, marked, skipped, strength, state):
    assert supplementary.session_state(
        ticked, total, marked_done=marked, skipped=skipped, strength_activity=strength
    ) == state


def test_checks_survive_a_revision_that_reorders_items_and_moves_follow_the_athlete():
    conn = memory_db()
    plan_id = _plan(conn)
    wed = _day(0, 2)

    s = supplementary.record(conn, plan_id, wed, 1, check=["Plank", "Jump rope"])
    assert s["state"] == "partial" and [i["checked"] for i in s["items"]] == [False, True, True]
    s = supplementary.record(conn, plan_id, wed, 1, uncheck=["Jump rope"])
    assert [i["name"] for i in s["items"] if i["checked"]] == ["Plank"]

    version_id = plan.start_revision_draft(conn, plan_id)
    plan.upsert_draft_days(conn, version_id, [
        {"date": wed, "slot": "supplementary", "target": {"kind": "strength", "items": LIFT[::-1]}},
    ])
    plan.commit_plan(conn, plan_id, note="reorder")
    assert [(i["name"], i["checked"]) for i in _session(conn, plan_id, wed)["items"]] == [
        ("Jump rope", False), ("Plank", True), ("Goblet squat", False),
    ]

    # Any day to any day; the latest move wins; the planned date puts it back.
    far = _day(1, 4)
    assert supplementary.record(conn, plan_id, wed, 1, move_to=far)["effective_date"] == far
    assert supplementary.record(conn, plan_id, wed, 1, move_to=_day(0, 3))["effective_date"] == _day(0, 3)
    back = supplementary.record(conn, plan_id, wed, 1, move_to=wed)
    assert (back["moved_to"], back["effective_date"]) == (None, wed)

    with pytest.raises(plan.PlanValidationError, match="no items named"):
        supplementary.record(conn, plan_id, wed, 1, check=["Bench press"])
    with pytest.raises(plan.PlanValidationError, match="no supplementary session"):
        supplementary.record(conn, plan_id, _day(0, 3), 1, done=True)


def test_a_strength_activity_finishes_a_strength_session_on_its_effective_day_only():
    conn = memory_db()
    plan_id = _plan(conn)
    wed, thu = _day(-1, 2), _day(-1, 3)
    insert_activity(conn, 1, start_date=f"{thu}T07:00:00", sport_type="WeightTraining")
    assert _session(conn, plan_id, wed)["state"] == "open"
    assert supplementary.record(conn, plan_id, wed, 1, move_to=thu)["state"] == "done"

    mobility = memory_db()
    mobility_plan = _plan(mobility, kind="mobility")
    insert_activity(mobility, 1, start_date=f"{wed}T07:00:00", sport_type="WeightTraining")
    assert _session(mobility, mobility_plan, wed)["state"] == "open"


def test_the_plan_api_serves_sessions_and_writes_stay_out_of_annotations():
    conn = memory_db()
    plan_id = _plan(conn)
    wed = _day(0, 2)
    plan_api._conn = lambda: conn  # type: ignore[assignment]
    session = plan_api.update_session(plan_id, {"date": wed, "seq": 1, "check": ["Plank"], "move_to": _day(0, 4)})
    assert session["effective_date"] == _day(0, 4)
    served = plan_api.get_plan(plan_id)
    assert served is not None
    assert any(s["date"] == wed and s["moved_to"] == _day(0, 4) for s in served["supplementary"])
    assert served["log"] == {}
    with pytest.raises(HTTPException) as err:
        plan_api.update_session(plan_id, {"date": wed, "seq": 1, "check": ["Nope"]})
    assert err.value.status_code == 400


def test_supplementary_days_are_validated_and_never_count_as_mileage():
    conn = memory_db()
    plan_id, version_id = plan.start_plan_draft(conn, title="P")
    day = {"date": _day(1, 0), "slot": "supplementary"}
    for bad, message in (
        ({}, "needs a target kind"),
        ({"target": {"kind": "Not A Kind!!"}}, "needs a target kind"),
        ({"target": {"kind": "mobility", "items": [{"name": "a"}, {"name": "a"}]}}, "duplicate item"),
        ({"target": {"kind": "mobility", "items": [{"amount": "x"}]}}, "name"),
        ({"slot": "easy", "target": {"kind": "mobility"}}, "supplementary sessions only"),
    ):
        with pytest.raises(plan.PlanValidationError, match=message):
            plan.upsert_draft_days(conn, version_id, [{**day, **bad}])
    plan.upsert_draft_days(conn, version_id, [{**day, "target": {"kind": " Mobility "}}])
    assert plan.get_draft(conn, plan_id)["days"][0]["target_json"] == '{"kind": "mobility"}'
    assert "supplementary" not in plan.MILEAGE_SLOTS


def test_the_goal_counts_sessions_by_the_week_they_were_done_in():
    conn = memory_db()
    goal = {
        "key": "lifts", "why": "consistency", "horizon": "weekly", "target": 1,
        "type": "supplementary_sessions", "params": {"kind": "strength", "partial": False},
    }
    plan_id = _plan(conn, goal_list=[goal, {**goal, "key": "any", "params": {"kind": "strength", "partial": True}}])
    # Week -2's session is finished a week late (moved into week -1); week -1's is half done.
    supplementary.record(conn, plan_id, _day(-2, 2), 1, done=True, move_to=_day(-1, 5))
    supplementary.record(conn, plan_id, _day(-1, 2), 1, check=["Plank"])
    values = {
        g["key"]: [w["value"] for w in g["weeks"] if w["value"] is not None]
        for g in goals.plan_goal_results(conn, plan_id)["goals"]
    }
    assert values["lifts"] == [0, 0, 1]
    assert values["any"] == [0, 0, 2]


def test_the_mcp_tools_read_and_write_sessions(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    plan_id = _plan(conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731
    wed = _day(0, 2)

    moved = tool("update_supplementary_session", session_date=wed, check=["Plank"], move_to=_day(0, 5), plan_id=plan_id)
    assert moved["state"] == "partial" and moved["effective_date"] == _day(0, 5)
    # Found by its effective date even when the planned date is outside the range.
    found = tool("get_supplementary_sessions", start=_day(0, 5), end=_day(0, 6), plan_id=plan_id)["sessions"]
    assert [s["date"] for s in found] == [wed]
    assert "error" in tool("update_supplementary_session", session_date=wed, uncheck=["Nope"], plan_id=plan_id)
