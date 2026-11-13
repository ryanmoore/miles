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


def test_a_revision_after_the_race_can_add_the_current_week_without_losing_its_days():
    conn = memory_db()
    raced = _plan(conn, "Spring 5K", -4, -1, race_week=-1)
    version_id = plan.start_revision_draft(conn, raced)
    plan.upsert_draft_weeks(conn, version_id, _weeks(0, 0))
    plan.upsert_draft_days(conn, version_id, [{"date": THIS_MONDAY.isoformat(), "slot": "easy", "target_miles": 30.0}])
    plan.commit_plan(conn, raced, note="add the recovery week")

    assert [d["date"] for d in plan.get_version(conn, version_id)["days"]] == [THIS_MONDAY.isoformat()]
    assert plan.current_plan(conn, date.today())["plan_id"] == raced


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


def test_a_mid_week_revision_changes_this_week_and_judges_it_but_an_ended_week_stays_fixed():
    conn = memory_db()
    plan_id = _plan(conn, "Cycle", -3, 2)
    version_id = plan.start_revision_draft(conn, plan_id)
    plan.upsert_draft_weeks(conn, version_id, [{**w, "target_miles": 99.0} for w in _weeks(-1, 1)])
    plan.commit_plan(conn, plan_id, note="reality changed")

    def target(week: int) -> float:
        monday = THIS_MONDAY + timedelta(weeks=week)
        bundle = plan.current_version_for_week(conn, plan_id, monday)
        assert bundle is not None
        return next(w["target_miles"] for w in bundle["weeks"] if w["week_start"] == monday.isoformat())

    assert [target(w) for w in (-1, 0, 1)] == [29.0, 99.0, 99.0]  # last week kept; this and next take the draft

    db.set_meta(conn, "last_sync_at", f"{(THIS_MONDAY + timedelta(weeks=1)).isoformat()}T09:00:00+00:00")
    used = {r["week_start"]: r["version_n_used"] for r in plan_adherence.compute_plan_adherence(conn)}
    assert (used[(THIS_MONDAY - timedelta(weeks=1)).isoformat()], used[THIS_MONDAY.isoformat()]) == (1, 2)

    conn.execute(
        "UPDATE plan_versions SET committed_at = ? WHERE version_id = ?",
        (f"{(THIS_MONDAY + timedelta(weeks=1)).isoformat()}T00:30:00+00:00", version_id),
    )
    assert target(0) == 30.0  # committed after the week ended, so it no longer judges it


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


_STEP = {"work": "3 min", "reps": 7, "effort": "sub-threshold", "rest": "60 s", "rest_mode": "jog"}


def test_steps_and_rationale_survive_commit_and_a_revision():
    conn = memory_db()
    plan_id, version_id = plan.start_plan_draft(conn, title="Base")
    plan.upsert_draft_weeks(conn, version_id, _weeks(0, 0))
    day: plan.DayInput = {
        "date": THIS_MONDAY.isoformat(), "slot": "workout", "target_miles": 30.0,
        "target": {"steps": [_STEP, {"work": "10 min", "label": "Warm-up"}]},
        "note": "keep it relaxed", "rationale": "builds volume at threshold",
    }
    plan.upsert_draft_days(conn, version_id, [day])
    plan.commit_plan(conn, plan_id, note="ok")

    revised = plan.start_revision_draft(conn, plan_id)
    for vid in (version_id, revised):
        (row,) = plan.get_version(conn, vid)["days"]
        assert row["rationale"] == "builds volume at threshold"
        assert '"steps"' in (row["target_json"] or "")
    assert plan_api._day_out(row)["rationale"] == "builds volume at threshold"


@pytest.mark.parametrize("target, message", [
    ({"steps": "3 min"}, "list of at most 12"),
    ({"steps": [{"work": "3 min"}] * 13}, "list of at most 12"),
    ({"steps": [{"reps": 3}]}, "step 1 needs a non-empty work"),
    ({"steps": [_STEP, {"work": "3 min", "pace": "fast"}]}, "step 2 has unknown keys"),
    ({"steps": [{"work": "3 min", "rest_mode": "crawl"}]}, "rest_mode must be one of"),
    ({"steps": [{"work": "x" * 41}]}, "work must be a string of at most 40"),
    ({"steps": [{"work": "3 min", "cue": "x" * 101}]}, "cue must be a string of at most 100"),
    ({"steps": [{"work": "3 min", "reps_lo": 5, "reps_hi": 3}]}, "reps_lo must be <= reps_hi"),
    ({"steps": [{"work": "3 min", "sets": -1}]}, "sets must be a non-negative integer"),
    ({"steps": [{"work": "3 min", "optional": "yes"}]}, "optional must be true or false"),
])
def test_invalid_steps_are_rejected_naming_the_day_and_step(target, message):
    conn = memory_db()
    _, version_id = plan.start_plan_draft(conn, title="Base")
    day: plan.DayInput = {"date": THIS_MONDAY.isoformat(), "slot": "workout", "title": "Reps", "target": target}
    with pytest.raises(plan.PlanValidationError, match=message) as e:
        plan.upsert_draft_days(conn, version_id, [day])
    assert "Reps" in str(e.value)


def test_sketches_expire_unless_written_or_committed_and_drafts_have_a_view(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(plan_api, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731

    sketch = tool("start_plan_draft", title="Sketch", sketch=True)
    plain = tool("start_plan_draft", title="Candidate")
    sid, pid = sketch["plan"]["plan_id"], plain["plan"]["plan_id"]
    assert sketch["url"].endswith(f"/plan.html?plan={sid}&draft=1#plan")
    assert sketch["expires_at"] and plain["expires_at"] is None
    assert {p["plan_id"]: p["role"] for p in tool("list_plans")} == {sid: "sketch", pid: "candidate"}

    stale = "2000-01-01T00:00:00+00:00"
    conn.execute("UPDATE plans SET sketch_expires_at = ? WHERE plan_id = ?", [stale, sid])
    assert tool("set_draft_weeks", weeks=_weeks(0, 0), plan_id=sid)["expires_at"] > stale
    conn.execute("UPDATE plans SET sketch_expires_at = ? WHERE plan_id = ?", [stale, sid])
    assert [p["plan_id"] for p in tool("list_plans")] == [pid]

    kept = tool("start_plan_draft", title="Kept", sketch=True)["plan"]["plan_id"]
    tool("set_draft_weeks", weeks=_weeks(0, 0), plan_id=kept)
    tool("commit_plan", note="ok", plan_id=kept)
    conn.execute("UPDATE plans SET sketch_expires_at = ? WHERE plan_id = ?", [stale, kept])
    assert {p["plan_id"]: p["role"] for p in tool("list_plans")} == {pid: "candidate", kept: "current"}

    tool("start_revision_draft", plan_id=kept)
    assert plan_api.get_plan(plan_id=kept, draft=True)["version"]["committed_at"] is None
    assert plan_api.get_plan(plan_id=kept)["version"]["committed_at"] is not None
    tool("discard_draft", plan_id=kept)
    with pytest.raises(HTTPException, match="no draft"):
        plan_api.get_plan(plan_id=kept, draft=True)


def test_compare_plans_aligns_by_date_when_weeks_overlap_else_by_position(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    monkeypatch.setattr(plan_api, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731
    mon = lambda w: (THIS_MONDAY + timedelta(weeks=w)).isoformat()  # noqa: E731

    base = _plan(conn, "Base", 0, 2)
    plan.upsert_draft_days(conn, plan.start_revision_draft(conn, base), [
        {"date": mon(1), "slot": "workout", "target_miles": 8.0, "title": "Hills"},
        {"date": (THIS_MONDAY + timedelta(weeks=1, days=1)).isoformat(), "slot": "easy", "target_miles": 5.0},
        {"date": (THIS_MONDAY + timedelta(weeks=1, days=2)).isoformat(), "slot": "easy", "target_miles": 8.0},
    ])
    other = _plan(conn, "Later", 10, 11, commit=False)
    (other_draft,) = conn.execute("SELECT version_id FROM plan_versions WHERE plan_id = ?", [other]).fetchone()
    plan.upsert_draft_days(conn, other_draft, [
        {"date": (THIS_MONDAY + timedelta(weeks=10, days=1)).isoformat(), "slot": "easy", "target_miles": 5.2},
        {"date": (THIS_MONDAY + timedelta(weeks=10, days=2)).isoformat(), "slot": "easy", "target_miles": 9.0},
        {"date": (THIS_MONDAY + timedelta(weeks=10, days=3)).isoformat(), "slot": "easy", "target_miles": 12.0},
    ])
    insert_activity(conn, 1, start_date=f"{mon(0)}T07:00:00", run_type="long_run", distance_m=16093.4)

    # Date alignment needs every other side to overlap the baseline; the actual side shares only week 0.
    by_date = tool("compare_plans", sides=[
        {"plan_id": base, "from_week": mon(0), "to_week": mon(2)},
        {"plan_id": base, "draft": True, "from_week": mon(1), "to_week": mon(3)},
        {"actual": True, "from_week": mon(0), "to_week": mon(0)},
    ])
    assert f"plan={base}&weeks={mon(0)}..{mon(2)}&vs={base}:draft:{mon(1)}..{mon(3)}&vs=actual:{mon(0)}..{mon(0)}" in by_date["url"]
    assert [w["week"] for w in by_date["weeks"]] == [mon(0), mon(1), mon(2)]
    week0, week1 = by_date["weeks"][0]["sides"], by_date["weeks"][1]["sides"]
    assert week0[1] is None and week0[2]["miles"] == 10.0 and "day_differences" not in week0[0]
    assert week1[1]["miles"] == 21.0 and week1[2] is None  # the days' sum, not the 31 mi target
    assert week1[1]["day_differences"] == [
        {"day": "Mon", "baseline": "rest", "side": "workout 8 mi Hills"},
        {"day": "Tue", "baseline": "rest", "side": "easy 5 mi"},
        {"day": "Wed", "baseline": "rest", "side": "easy 8 mi"},
    ]
    assert week0[2]["day_differences"][0] == {"day": "Mon", "baseline": "rest", "side": "long 10 mi"}
    assert by_date["shared_weeks_note"] == f"Figures cover the 2 weeks all plans share, {mon(1)} to {mon(2)}"
    assert [(s["weeks"], s["total_miles"]) for s in by_date["sides"]] == [(3, 63.0), (2, 53.0), (1, 0.0)]

    # Pairing is by position unless every other side overlaps the baseline.
    ordinal = tool("compare_plans", sides=[
        {"plan_id": base, "draft": True, "from_week": mon(1), "to_week": mon(2)},
        {"plan_id": other, "draft": True},
        {"plan_id": base, "from_week": mon(1), "to_week": mon(2)},
    ])
    assert [(w["week"], [c["week_start"] for c in w["sides"]]) for w in ordinal["weeks"]] == [
        ("Week 1", [mon(1), mon(10), mon(1)]), ("Week 2", [mon(2), mon(11), mon(2)]),
    ]
    # 5 vs 5.2 mi and 8 vs 9 mi are negligible, 8 vs 12 is not; Monday's session is on one side only.
    assert [d["day"] for d in ordinal["weeks"][0]["sides"][1]["day_differences"]] == ["Mon", "Thu"]
    assert [d["day"] for d in ordinal["weeks"][0]["sides"][2]["day_differences"]] == ["Mon", "Tue", "Wed"]

    tuesday = (THIS_MONDAY + timedelta(days=1)).isoformat()
    assert "not a Monday" in tool("compare_plans", sides=[{"plan_id": base, "from_week": tuesday, "to_week": tuesday}, {"plan_id": other}])["error"]


def _plan_with_days(conn, title: str, first: int, last: int, *, commit: bool = True) -> int:
    """A plan whose weeks each hold one easy day filling the target and a Wednesday strength day."""
    plan_id, version_id = plan.start_plan_draft(conn, title=title, sketch=not commit)
    plan.upsert_draft_weeks(conn, version_id, _weeks(first, last))
    plan.upsert_draft_days(conn, version_id, [
        d for w in range(first, last + 1) for d in (
            {"date": (THIS_MONDAY + timedelta(weeks=w)).isoformat(), "slot": "easy", "target_miles": 30.0 + w},
            {"date": (THIS_MONDAY + timedelta(weeks=w, days=2)).isoformat(), "slot": "strength", "title": "Lift"},
        )
    ])
    if commit:
        plan.commit_plan(conn, plan_id, note="ok")
    return plan_id


def test_copy_weeks_between_plans_and_compact_write_responses(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731
    mon = lambda w: (THIS_MONDAY + timedelta(weeks=w)).isoformat()  # noqa: E731

    base = _plan_with_days(conn, "Base", 10, 14)
    sketch = tool("start_plan_draft", title="Sketch", sketch=True)["plan"]["plan_id"]

    # A copy is exact, strength sessions included, and acknowledges only the weeks it touched.
    ack = tool("copy_weeks_to_draft", from_plan_id=base, plan_id=sketch)
    assert ack["copied"]["weeks"] == [mon(w) for w in range(10, 15)]
    assert ack["plan"] == {"plan_id": sketch, "title": "Sketch", "role": "sketch"}
    assert ack["draft_weeks"] == {"count": 5, "first": mon(10), "last": mon(14)}
    assert len(ack["week_mileage"]) == 5 and not {"weeks", "days", "goals"} & ack.keys()
    assert tool("compare_plans", sides=[{"plan_id": base}, {"plan_id": sketch, "draft": True}])["weeks"][0]["sides"][1]["day_differences"] == []

    # Compact acknowledgements name only the touched weeks; get_draft has everything, or a range of it.
    tool("set_draft_days", days=[{"date": mon(11), "slot": "easy", "target_miles": 41.0}], plan_id=sketch)
    ack = tool("set_draft_weeks", weeks=[{**_weeks(12, 12)[0], "target_miles": 7.0}], plan_id=sketch)
    assert [c["week_start"] for c in ack["week_mileage"]] == [mon(12)] and ack["written"] == {"weeks": [mon(12)]}
    full = tool("get_draft", plan_id=sketch)
    assert len(full["weeks"]) == 5 and len(full["days"]) == 10 and len(full["week_mileage"]) == 5
    part = tool("get_draft", plan_id=sketch, from_week=mon(11), to_week=mon(12))
    assert [w["week_start"] for w in part["weeks"]] == [mon(11), mon(12)] and len(part["days"]) == 4
    assert [c["week_start"] for c in part["week_mileage"]] == [mon(11), mon(12)]
    assert [w["week_start"] for w in tool("get_training_plan", plan_id=base, from_week=mon(13))["weeks"]] == [mon(13), mon(14)]

    # An agreed sketch goes into a revision draft; only the copied range changes.
    tool("start_revision_draft", plan_id=base)
    tool("copy_weeks_to_draft", from_plan_id=sketch, from_draft=True, from_week=mon(11), to_week=mon(12), plan_id=base)
    revised = {w["week_start"]: w["target_miles"] for w in tool("get_draft", plan_id=base)["weeks"]}
    assert revised == {mon(10): 40.0, mon(11): 41.0, mon(12): 7.0, mon(13): 43.0, mon(14): 44.0}

    errors = {
        "unknown plan": tool("copy_weeks_to_draft", from_plan_id=999, plan_id=sketch),
        "no draft to write to": tool("copy_weeks_to_draft", from_plan_id=sketch, from_draft=True, plan_id=999),
        "empty range": tool("copy_weeks_to_draft", from_plan_id=base, from_week=mon(20), to_week=mon(21), plan_id=sketch),
        "into itself": tool("copy_weeks_to_draft", from_plan_id=sketch, from_draft=True, plan_id=sketch),
    }
    assert {k: v["error"] for k, v in errors.items()} == {
        "unknown plan": "plan 999 does not exist",
        "no draft to write to": "no draft in progress; call start_plan_draft or start_revision_draft first",
        "empty range": f"plan {base} has no weeks in that range",
        "into itself": "the source is the draft being written to",
    }


def test_compare_summaries_cover_the_shared_weeks_and_strength_only_days_are_listed(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731
    mon = lambda w: (THIS_MONDAY + timedelta(weeks=w)).isoformat()  # noqa: E731

    long_plan = _plan_with_days(conn, "Ten weeks", 0, 9)
    short = _plan_with_days(conn, "Nine weeks", 20, 28, commit=False)
    conn.execute("DELETE FROM plan_days WHERE version_id IN (SELECT version_id FROM plan_versions WHERE plan_id = ?) AND slot = 'strength'", [short])

    # Paired by position the shorter plan sets the count, so the totals compare like with like.
    by_position = tool("compare_plans", sides=[{"plan_id": long_plan}, {"plan_id": short, "draft": True}])
    assert by_position["shared_weeks_note"] == "Figures cover the 9 weeks all plans share"
    assert [(s["weeks"], s["total_miles"]) for s in by_position["sides"]] == [(10, 306.0), (9, 486.0)]
    assert len(by_position["weeks"]) == 10
    # The strength session on the baseline's Wednesday is listed and reads as strength.
    wednesday = [d for d in by_position["weeks"][0]["sides"][1]["day_differences"] if d["day"] == "Wed"]
    assert wednesday == [{"day": "Wed", "baseline": "strength Lift", "side": "rest"}]

    # Paired by date the shared range is named, and equal coverage shows no note.
    by_date = tool("compare_plans", sides=[{"plan_id": long_plan}, {"plan_id": long_plan, "from_week": mon(1), "to_week": mon(9)}])
    assert by_date["shared_weeks_note"] == f"Figures cover the 9 weeks all plans share, {mon(1)} to {mon(9)}"
    assert tool("compare_plans", sides=[{"plan_id": long_plan}, {"plan_id": long_plan}])["shared_weeks_note"] is None


def test_compare_counts_a_week_at_its_days_sum_and_falls_back_to_its_target(monkeypatch):
    conn = memory_db()
    monkeypatch.setattr(mcp_server, "_conn", lambda: conn)
    tool = lambda name, **args: call_tool(mcp_server.mcp, name, args)  # noqa: E731
    day = lambda w, i: (THIS_MONDAY + timedelta(weeks=w, days=i)).isoformat()  # noqa: E731

    plan_id, version_id = plan.start_plan_draft(conn, title="Ranges")
    plan.upsert_draft_weeks(conn, version_id, [
        {"week_start": day(w, 0), "target_miles": 70.0, "target_miles_hi": 75.0, "target_workouts": 1, "phase": "base"} for w in range(3)
    ])
    plan.upsert_draft_days(conn, version_id, [
        {"date": day(0, 0), "slot": "easy", "target_miles": 40.0},
        {"date": day(0, 1), "slot": "long", "target_miles": 34.0},
        {"date": day(2, 0), "slot": "easy", "target_miles": 40.0},
        {"date": day(2, 1), "slot": "long", "target_minutes": 120.0},
    ])
    plan.commit_plan(conn, plan_id, note="ok")

    weeks = tool("compare_plans", sides=[{"plan_id": plan_id}, {"plan_id": plan_id}])["weeks"]
    assert [(w["sides"][0]["miles"], w["sides"][0]["miles_is_target"]) for w in weeks] == [(74.0, False), (72.5, True), (72.5, True)]
