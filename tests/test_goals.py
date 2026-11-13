import sqlite3
from datetime import date, timedelta

import pytest
from helpers import call_tool, insert_activity, insert_terrain_segments

from miles import db, goals, history, mcp_server, notes, plan, plan_api
from miles.fitness import MILE_M
from miles.periods import monday_of

THIS_MONDAY = monday_of(date.today())
STRIDES = [(250, 3.2, 0.0)] + [p for _ in range(5) for p in ((6, 5.8, 0.0), (24, 3.2, 0.0))] + [(20, 3.2, 0.0)]


def _monday(weeks_from_now: int) -> str:
    return (THIS_MONDAY + timedelta(weeks=weeks_from_now)).isoformat()


def _goal(key: str = "miles", **fields: object) -> dict[str, object]:
    return {"key": key, "why": "the cycle's emphasis", "horizon": "weekly", "type": "weekly_miles", "params": {}, "target": 30, **fields}


@pytest.fixture
def conn():
    connection = db.connect()
    yield connection
    connection.close()


def _plan(conn: sqlite3.Connection, goal_list: list[dict[str, object]], first: int = -4, last: int = 1, commit: bool = True) -> tuple[int, int]:
    """A plan over weeks first..last relative to this week, with these goals."""
    plan_id, version_id = plan.start_plan_draft(conn, title="Cycle")
    plan.upsert_draft_weeks(conn, version_id, [
        {"week_start": _monday(w), "target_workouts": 1, "phase": "base"} for w in range(first, last + 1)
    ])
    goals.set_draft_goals(conn, version_id, goal_list)
    if commit:
        plan.commit_plan(conn, plan_id, note="ok")
    return plan_id, version_id


def _miles_by_week(conn: sqlite3.Connection, miles: list[float], first: int = -4) -> None:
    for i, m in enumerate(miles):
        insert_activity(conn, 100 + i, start_date=f"{_monday(first + i)}T08:00:00", distance_m=m * MILE_M)


def _values(conn: sqlite3.Connection, plan_id: int, key: str) -> list[float | None]:
    result = next(g for g in goals.plan_goal_results(conn, plan_id)["goals"] if g["key"] == key)
    return [w["value"] for w in result["weeks"]]


def test_each_goal_type_counts_what_it_names_in_a_week(conn):
    week = -2
    day = lambda n: f"{(THIS_MONDAY + timedelta(weeks=week, days=n)).isoformat()}T08:00:00"  # noqa: E731
    insert_activity(conn, 1, start_date=day(0), distance_m=3 * MILE_M)
    insert_activity(conn, 2, start_date=day(1), distance_m=5 * MILE_M)
    insert_activity(conn, 3, start_date=day(2), distance_m=4 * MILE_M)
    insert_activity(conn, 4, start_date=day(3), distance_m=2 * MILE_M)
    for activity_id, n, sport in ((10, 0, "WeightTraining"), (11, 0, "WeightTraining"), (12, 2, "Workout"), (13, 3, "Ride")):
        insert_activity(conn, activity_id, start_date=day(n), sport_type=sport)
    insert_terrain_segments(conn, 2, day(1)[:10], STRIDES)
    insert_terrain_segments(conn, 3, day(2)[:10], STRIDES)
    notes.set_run_note(conn, 1, {"short": "a", "kind": "long_run", "tags": ["Strides"], "elements": [{"type": "strides", "count": 4, "source": "athlete"}]})
    notes.set_run_note(conn, 2, {"short": "b", "kind": "threshold", "elements": [{"type": "reps", "count": 6, "source": "laps"}]})
    notes.set_run_note(conn, 3, {"short": "c", "elements": [{"type": "strides", "count": 0, "source": "athlete"}]})

    plan_id, _ = _plan(conn, [
        _goal("miles", target=0),
        _goal("strides", type="element_sessions", params={"types": ["strides", "hill_sprints"]}, target=0),
        _goal("tagged", type="tagged_runs", params={"tag": "strides"}, target=0),
        _goal("kinds", type="run_kind", params={"kinds": ["long_run", "threshold"]}, target=0),
        _goal("lifting", type="activity_days", params={"sport_types": ["WeightTraining", "Workout"]}, target=0),
    ])

    at_week = lambda key: _values(conn, plan_id, key)[2]  # noqa: E731
    assert at_week("miles") == 14.0
    assert at_week("strides") == 2  # the noted run, and the stride finish after a workout the note lists only reps for; not the rejected detection
    assert at_week("tagged") == 1
    assert at_week("kinds") == 2
    assert at_week("lifting") == 2  # two distinct days; the ride does not count
    assert _values(conn, plan_id, "miles")[-2:] == [None, None]  # this week and next are not scored


@pytest.mark.parametrize(
    "noted, detected, expected",
    [
        (None, [("strides", 6)], 6),
        ([("reps", 6)], [("strides", 6)], 6),  # strides after a workout survive a note that lists only the reps
        ([("strides", 4)], [("strides", 6)], 4),  # the note wins
        ([("strides", 0)], [("strides", 6)], 0),  # count 0 rejects a false detection
        ([("strides", 4)], [("hill_sprints", 5)], 9),  # per type, not per run
    ],
)
def test_a_note_overrides_detection_per_element_type(noted, detected, expected):
    as_notes = [{"type": t, "count": c} for t, c in noted] if noted is not None else None
    assert history._strides(as_notes, [{"type": t, "count": c} for t, c in detected]) == expected  # type: ignore[list-item]


@pytest.mark.parametrize(
    "miles, overrides, flagged_weeks",
    [
        ([35, 20, 35, 35], {}, 0),  # one week under is not a pattern
        ([35, 35, 20, 20], {}, 2),
        ([35, 20, 20, 20], {}, 3),
        ([20, 20, 35, 35], {}, 0),  # resolved: only a running pattern flags
        ([35, 35, 20, 20], {-1: 0}, 0),  # the override lowers the last week's target
    ],
)
def test_a_flagged_weekly_goal_flags_two_or_more_consecutive_weeks_under(conn, miles, overrides, flagged_weeks):
    _miles_by_week(conn, miles)
    plan_id, _ = _plan(conn, [_goal(flag=True, overrides={_monday(w): t for w, t in overrides.items()})])

    flags = next(iter(goals.plan_goal_results(conn, plan_id)["goals"]))["flags"]

    assert [f["weeks"] for f in flags] == ([flagged_weeks] if flagged_weeks else [])


def test_a_week_the_sync_has_not_covered_has_no_value_and_does_not_extend_a_pattern(conn):
    _miles_by_week(conn, [35, 20, 20])
    db.set_meta(conn, "last_sync_at", f"{_monday(-1)}T09:00:00+00:00")
    plan_id, _ = _plan(conn, [_goal(flag=True)])

    result = goals.plan_goal_results(conn, plan_id)["goals"][0]

    assert [w["value"] for w in result["weeks"]][:5] == [35.0, 20.0, 20.0, None, None]
    assert [f["weeks"] for f in result["flags"]] == [2]


def test_cycle_goals_roll_up_scored_weeks_and_report_progress(conn):
    _miles_by_week(conn, [62, 58, 65, 70])
    plan_id, _ = _plan(conn, [
        _goal("over60", horizon="cycle", aggregate="weeks_at_least", threshold=60, target=3),
        _goal("eight", horizon="cycle", aggregate="weeks_at_least", threshold=60, target=6),
        _goal("avg", horizon="cycle", aggregate="average", target=60),
        _goal("avg_span", horizon="cycle", aggregate="average", target=70, from_week=_monday(-3), to_week=_monday(-1)),
    ])

    progress = {g["key"]: g["progress"] for g in goals.plan_goal_results(conn, plan_id)["goals"]}

    assert progress["over60"] == {"so_far": 3.0, "scored": 4, "left": 2, "met": True, "reachable": True}
    assert progress["eight"] == {"so_far": 3.0, "scored": 4, "left": 2, "met": False, "reachable": False}
    assert progress["avg"]["so_far"] == 63.8 and progress["avg"]["met"]
    assert progress["avg_span"] == {"so_far": 64.3, "scored": 3, "left": 0, "met": False, "reachable": None}


def test_a_revision_copies_goals_and_each_week_is_judged_by_the_goals_that_governed_it(conn):
    _miles_by_week(conn, [12, 12, 12, 12])
    plan_id, _ = _plan(conn, [_goal(target=10, flag=True)])
    version_id = plan.start_revision_draft(conn, plan_id)
    assert plan.get_draft(conn, plan_id)["goals"][0]["target"] == 10

    goals.set_draft_goals(conn, version_id, [_goal(target=100, flag=True)])
    plan.commit_plan(conn, plan_id, note="raise it")

    result = goals.plan_goal_results(conn, plan_id)["goals"][0]
    judged = [w for w in result["weeks"] if w["value"] is not None]
    assert [(w["target"], w["met"]) for w in judged] == [(10, True)] * 4  # version 1 governed these weeks
    assert result["weeks"][-1]["target"] == 100  # the weeks ahead show the new target
    assert plan.diff_versions(conn, version_id - 1, version_id)["changed_goals"] == [{"key": "miles", "change": "changed"}]


def test_a_mid_week_goal_revision_judges_this_week(conn):
    _miles_by_week(conn, [12, 12, 12, 12, 12])
    plan_id, _ = _plan(conn, [_goal(target=10)])
    version_id = plan.start_revision_draft(conn, plan_id)
    goals.set_draft_goals(conn, version_id, [_goal(target=100)])
    plan.commit_plan(conn, plan_id, note="raise it")
    db.set_meta(conn, "last_sync_at", f"{_monday(1)}T09:00:00+00:00")

    result = goals.plan_goal_results(conn, plan_id)["goals"][0]
    assert [w["target"] for w in result["weeks"][3:6]] == [10, 100, 100]  # last week ended before the revision


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"type": "nope"}, "type must be one of"),
        ({"params": {"types": ["strides"]}}, "takes params"),
        ({"type": "element_sessions", "params": {"types": ["stride"]}}, "must each be one of"),
        ({"target": -1}, "target must be a number >= 0"),
        ({"why": ""}, "why is required"),
        ({"overrides": {(THIS_MONDAY + timedelta(days=1)).isoformat(): 0}}, "is not a Monday"),
        ({"overrides": {_monday(40): 0}}, "outside the plan's weeks"),
        ({"aggregate": "average"}, "cycle goals only"),
        ({"horizon": "cycle"}, "needs aggregate"),
        ({"horizon": "cycle", "aggregate": "weeks_at_least"}, "threshold goes with weeks_at_least"),
        ({"horizon": "cycle", "aggregate": "average", "flag": True}, "weekly goals only"),
        ({"horizon": "cycle", "aggregate": "average", "from_week": _monday(1), "to_week": _monday(0)}, "from_week is after to_week"),
        ({"horizon": "cycle", "aggregate": "average", "to_week": _monday(40)}, "outside the plan's weeks"),
    ],
)
def test_set_draft_goals_rejects_malformed_goals(conn, fields, message):
    _, version_id = _plan(conn, [], commit=False)
    with pytest.raises(plan.PlanValidationError, match=message):
        goals.set_draft_goals(conn, version_id, [_goal(**fields)])


def test_commit_rejects_a_goal_pointing_outside_the_plan_weeks(conn):
    plan_id, version_id = _plan(conn, [_goal(overrides={_monday(1): 0})], commit=False)
    plan.delete_draft_weeks(conn, version_id, [_monday(1)])
    with pytest.raises(plan.PlanValidationError, match="outside the plan's weeks"):
        plan.commit_plan(conn, plan_id, note="ok")


def test_goals_are_set_and_read_over_mcp_and_http(conn, monkeypatch):
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731
    _miles_by_week(conn, [62, 58, 65, 70])
    plan_id, _ = _plan(conn, [], commit=False)

    bad = tool("set_draft_goals", goals=[_goal(type="nope")])
    assert "type must be one of" in bad["error"]
    tool("set_draft_goals", goals=[
        _goal("weekly", flag=True),
        _goal("cycle", horizon="cycle", aggregate="weeks_at_least", threshold=60, target=3),
    ])
    assert [g["key"] for g in tool("get_draft")["goals"]] == ["weekly", "cycle"]
    tool("commit_plan", note="ok")

    read = tool("get_plan_goals", plan_id=plan_id)
    weekly, cycle = read["goals"]
    assert (weekly["key"], weekly["why"], weekly["target"], weekly["flags"]) == ("weekly", "the cycle's emphasis", 30, [])
    assert [w["value"] for w in weekly["weeks"]] == [62, 58, 65, 70, None, None]
    assert cycle["progress"] == {"so_far": 3.0, "scored": 4, "left": 2, "met": True, "reachable": True}
    assert plan_api.get_plan_goals(plan_id) == read
    assert tool("get_training_plan", plan_id=plan_id)["goals"][0]["key"] == "weekly"
