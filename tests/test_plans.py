from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from helpers import call_tool, insert_activity, memory_db

from miles import db, mcp_server, plan, plan_adherence, plan_api
from miles.periods import monday_of

THIS_MONDAY = monday_of(date.today())


def _sunday(weeks_from_now: int) -> str:
    return (THIS_MONDAY + timedelta(weeks=weeks_from_now, days=6)).isoformat()


def _weeks(first: int, last: int) -> list[plan.WeekInput]:
    return [
        {"week_start": (THIS_MONDAY + timedelta(weeks=w)).isoformat(), "target_miles": 30.0 + w, "target_workouts": 1, "phase": "base"}
        for w in range(first, last + 1)
    ]


def _plan(conn, title: str, first: int, last: int, *, race_week: int | None = None, commit: bool = True) -> int:
    """A plan covering weeks first..last relative to this week, racing a 10K on the Sunday of race_week."""
    race = {"race_date": _sunday(race_week), "distance_bucket": "10K"} if race_week is not None else {}
    plan_id, version_id = plan.start_plan_draft(conn, title=title, **race)
    plan.upsert_draft_weeks(conn, version_id, _weeks(first, last))
    if commit:
        plan.commit_plan(conn, plan_id, note="ok")
    return plan_id


def _race_run(conn, activity_id: int, week: int) -> None:
    insert_activity(
        conn, activity_id, start_date=f"{_sunday(week)}T08:00:00",
        workout_type=1, run_type="race", distance_m=10000.0, moving_time_s=2400,
    )


def _revise(conn, plan_id: int, first: int, last: int, **race) -> int:
    version_id = plan.start_revision_draft(conn, plan_id)
    plan.upsert_draft_weeks(conn, version_id, _weeks(first, last))
    if race:
        plan.set_draft_race(conn, version_id, **race)
    plan.commit_plan(conn, plan_id, note="revise")
    return version_id


def test_a_revision_carries_its_race_and_a_changed_race_moves_the_week_requirement():
    conn = memory_db()
    plan_id = _plan(conn, "Spring 5K", 20, 25, race_week=25)

    version_id = plan.start_revision_draft(conn, plan_id)
    assert plan.get_draft(conn, plan_id)["version"]["race_date"] == _sunday(25)
    plan.set_draft_race(conn, version_id, _sunday(27), "10K")
    assert any("race week" in g for g in plan.get_draft(conn, plan_id)["gaps"])
    with pytest.raises(plan.PlanValidationError, match="must include the race week"):
        plan.commit_plan(conn, plan_id, note="move race")

    plan.upsert_draft_weeks(conn, version_id, _weeks(26, 27))
    plan.commit_plan(conn, plan_id, note="move race")

    assert plan.get_plan(conn, plan_id)["race_date"] == _sunday(27)
    diff = plan.diff_versions(conn, version_id - 1, version_id)
    assert diff["changed_race"] == {"race_date": (_sunday(25), _sunday(27))}


def test_a_plan_without_a_race_commits_with_any_end_and_a_revision_can_drop_the_race():
    conn = memory_db()
    plan_id = _plan(conn, "Base block", 20, 23)
    assert plan.get_plan(conn, plan_id)["race_date"] is None

    raced = _plan(conn, "Fall 50K", 30, 35, race_week=33)  # extends two weeks past its race
    assert plan.get_plan(conn, raced)["last_week"] == (THIS_MONDAY + timedelta(weeks=35)).isoformat()

    _revise(conn, raced, 30, 35, race_date=None)
    assert plan.get_plan(conn, raced)["race_date"] is None


def test_committed_plans_may_not_share_a_week_but_adjacent_plans_and_candidates_coexist():
    conn = memory_db()
    current = _plan(conn, "Current", -2, 3, race_week=2)
    nxt = _plan(conn, "Next", 4, 9)  # starts the week after Current ends: no overlap
    cand_a = _plan(conn, "Candidate A", 4, 9, commit=False)
    cand_b = _plan(conn, "Candidate B", 4, 9, commit=False)

    with pytest.raises(plan.PlanValidationError, match=rf"already covered by plan {nxt} \('Next'\)") as err:
        plan.commit_plan(conn, cand_a, note="ok")
    assert (THIS_MONDAY + timedelta(weeks=4)).isoformat() in str(err.value)

    plan.discard_draft(conn, cand_a)
    with pytest.raises(plan.PlanValidationError, match="already covered"):
        _revise(conn, current, -2, 4)  # extending Current into Next's first week

    conn.execute("UPDATE plans SET status = 'abandoned' WHERE plan_id = ?", [nxt])
    plan.commit_plan(conn, cand_b, note="ok")  # an abandoned plan no longer holds its weeks


def test_roles_follow_the_dates_and_a_gap_week_has_no_current_plan():
    conn = memory_db()
    past = _plan(conn, "Past", -12, -6, race_week=-6)
    nxt = _plan(conn, "Next", 2, 8)
    candidate = _plan(conn, "Candidate", 2, 8, commit=False)
    abandoned = _plan(conn, "Dropped", -4, -3)
    conn.execute("UPDATE plans SET status = 'abandoned' WHERE plan_id = ?", [abandoned])

    roles = {p["plan_id"]: plan.plan_role(p, date.today()) for p in plan.list_plans(conn)}
    assert roles == {past: "past", nxt: "next", candidate: "candidate", abandoned: "abandoned"}
    assert plan.current_plan(conn, date.today()) is None
    assert plan.default_plan(conn, date.today())["plan_id"] == past  # the Plan tab falls back to the latest past plan

    current = _plan(conn, "Current", -1, 1)
    assert plan.current_plan(conn, date.today())["plan_id"] == current
    assert plan.plan_role(plan.get_plan(conn, current), date.today() + timedelta(weeks=9)) == "past"


def test_a_race_week_inside_the_current_plan_does_not_end_it():
    conn = memory_db()
    current = _plan(conn, "Spring 5K", -4, 1, race_week=0)
    _race_run(conn, 1, 0)
    assert plan.auto_complete_plan(conn) == [current]
    row = plan.get_plan(conn, current)
    assert row["status"] == "completed"
    assert plan.plan_role(row, date.today()) == "current"  # this week is its post-race week


def test_auto_complete_checks_each_active_plan_against_its_own_race():
    conn = memory_db()
    first = _plan(conn, "First", -8, -3, race_week=-3)
    second = _plan(conn, "Second", -2, 3, race_week=0)
    no_race = _plan(conn, "Base", 10, 12)
    _race_run(conn, 1, 0)

    assert plan.auto_complete_plan(conn) == [second]
    assert {p["plan_id"]: p["status"] for p in plan.list_plans(conn)} == {first: "active", second: "completed", no_race: "active"}


def test_every_committed_plan_is_scored_through_its_last_week():
    conn = memory_db()
    past = _plan(conn, "Past", -8, -5, race_week=-6)
    current = _plan(conn, "Current", -4, 2)
    nxt = _plan(conn, "Next", 3, 6)
    db.set_meta(conn, "last_sync_at", f"{date.today().isoformat()}T09:00:00+00:00")

    scored: dict[int, int] = {}
    for row in plan_adherence.compute_plan_adherence(conn):
        scored[row["plan_id"]] = scored.get(row["plan_id"], 0) + 1
    assert scored == {past: 4, current: 4}  # Past's post-race weeks count; Next has not started


def test_plan_endpoints_default_to_the_current_plan_and_take_a_plan_id(monkeypatch):
    conn = memory_db()
    past = _plan(conn, "Past", -8, -5, race_week=-6)
    current = _plan(conn, "Current", -2, 3)
    cand = _plan(conn, "Candidate", 4, 9, commit=False)
    _race_run(conn, 1, -6)
    plan.auto_complete_plan(conn)
    conn.execute(
        "INSERT INTO plan_adherence (plan_id, week_start, version_n_used, actual_miles, band) VALUES (?, ?, 1, 30, 'on'), (?, ?, 1, 10, 'off')",
        [past, _weeks(-8, -8)[0]["week_start"], past, _weeks(-7, -7)[0]["week_start"]],
    )
    monkeypatch.setattr(plan_api, "_conn", lambda: conn)

    assert plan_api.get_plan()["plan"]["plan_id"] == current
    assert plan_api.get_plan()["today"] is not None
    past_view = plan_api.get_plan(plan_id=past)
    assert (past_view["role"], past_view["today"]) == ("past", None)
    assert plan_api.get_plan(plan_id=cand)["role"] == "candidate"
    assert [v["plan_id"] for v in plan_api.get_plan_versions(plan_id=cand)] == [cand]
    with pytest.raises(HTTPException):
        plan_api.get_plan(plan_id=999)

    rows = {r["plan_id"]: r for r in plan_api.get_plans()}
    assert [r["role"] for r in rows.values()] == ["past", "current", "candidate"]
    assert rows[past]["adherence"]["weeks_on_or_close"] == 1
    assert rows[past]["adherence"]["weeks_completed"] == 2
    assert rows[past]["race"] == {"date": _sunday(-6), "distance_bucket": "10K", "goal_time_s": None}
    assert rows[past]["result_time_s"] == 2400
    assert (rows[past]["peak_target_miles"], rows[past]["versions"]) == (25.0, 1)
    assert rows[cand]["first_week"] == (THIS_MONDAY + timedelta(weeks=4)).isoformat()
    assert rows[current]["race"] is None


def test_candidate_drafts_are_addressed_by_plan_id_and_listed(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731

    a = tool("start_plan_draft", title="Candidate A")["plan"]["plan_id"]
    b = tool("start_plan_draft", title="Candidate B")["plan"]["plan_id"]
    assert "pass plan_id" in tool("get_draft")["error"]

    tool("set_draft_weeks", weeks=_weeks(4, 7), plan_id=a)
    tool("set_draft_race", race_date=_sunday(7), distance_bucket="10K", plan_id=a)
    assert tool("get_draft", plan_id=a)["gaps"] == []
    assert tool("commit_plan", note="ok", plan_id=a)["plan"]["plan_id"] == a
    assert {p["plan_id"]: p["role"] for p in tool("list_plans")} == {a: "next", b: "candidate"}
