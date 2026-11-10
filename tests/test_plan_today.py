from datetime import date

import pytest
from helpers import insert_activity, memory_db

from miles import db
from miles.plan_api import _sync_cutoff_date, _today_blocks, get_today

TODAY = date(2026, 11, 1)  # a Sunday


def _day(d: str, miles: float, slot: str = "easy") -> dict:
    return {"date": d, "seq": 1, "slot": slot, "title": None, "target_miles": miles,
            "target_minutes": None, "target_json": None, "terrain": None, "note": None, "rationale": None}


def _blocks(conn):
    week = {"week_start": "2026-10-26", "target_miles": 20.0, "target_miles_hi": 20.0,
            "target_workouts": 1, "phase": "build", "note": None}
    days = [_day("2026-10-26", 4), _day("2026-10-27", 4), _day("2026-11-01", 12, "long_run")]
    plan = {"race_date": "2027-03-01"}
    cutoff = _sync_cutoff_date(conn, TODAY)
    _, week_so_far, vs_last_week = _today_blocks(conn, plan, [week], days, TODAY, cutoff)
    return week_so_far, vs_last_week


def _synced_conn(run_today: bool):
    conn = memory_db()
    db.set_meta(conn, "last_sync_at", "2026-11-01T09:00:00+00:00")
    insert_activity(conn, 1, start_date="2026-10-26T07:00:00", distance_m=4 * 1609.34)
    insert_activity(conn, 2, start_date="2026-10-25T07:00:00", distance_m=6 * 1609.34)
    if run_today:
        insert_activity(conn, 3, start_date="2026-11-01T07:00:00", distance_m=12 * 1609.34)
    return conn


@pytest.mark.parametrize(
    "run_today, expected, remaining, remaining_miles, cutoff_offset",
    [(False, 8.0, ["2026-11-01"], 12.0, 5), (True, 20.0, [], None, 6)],
)
def test_todays_planned_run_counts_only_once_synced(run_today, expected, remaining, remaining_miles, cutoff_offset):
    wsf, vs = _blocks(_synced_conn(run_today))
    assert wsf["expected_miles_by_now"] == expected
    assert [r["date"] for r in wsf["remaining_days"]] == remaining
    assert wsf["remaining_planned_miles"] == remaining_miles
    assert vs is not None and vs["cutoff_offset"] == cutoff_offset
    assert len(vs["days"]) == 7


def test_today_endpoint_compares_through_yesterday_without_a_run(monkeypatch):
    monkeypatch.setattr("miles.plan_api._conn", lambda: _synced_conn(run_today=False))
    monkeypatch.setattr("miles.plan_api.date", type("D", (date,), {"today": classmethod(lambda cls: TODAY)}))
    assert get_today()["vs_last_week"]["cutoff_offset"] == 5
