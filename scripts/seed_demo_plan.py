"""Seed a realistic 16-week marathon plan into a SCRATCH copy of the DB, for
visually reviewing plan.html's populated states (active plan, versions + diff,
plan_log markers, adherence bands, progression charts).

Usage:
    cp data/activities.db /tmp/plan_demo.db
    MILES_DB=/tmp/plan_demo.db uv run python scripts/seed_demo_plan.py
    MILES_DB=/tmp/plan_demo.db uv run uvicorn miles.api:app --port 8001

The plan window (WEEK1_MONDAY..RACE_DATE) should overlap real synced training
weeks so actual bars/calendar fills visibly overlay the planned targets —
adjust the dates below to the athlete's most recent training stretch.

SAFETY: refuses to run unless MILES_DB is set and points somewhere other than
the repo's real data/activities.db.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DB_ENV = os.environ.get("MILES_DB", "")
assert _DB_ENV, "refusing to run — set MILES_DB to a scratch copy of the DB"
assert Path(_DB_ENV).resolve() != (_REPO / "data" / "activities.db").resolve(), \
    "refusing to run against the real data/activities.db — use a scratch copy"

sys.path.insert(0, str(_REPO))

from miles import db, plan  # noqa: E402
from miles.derive import derive_all  # noqa: E402

WEEK1_MONDAY = date(2026, 3, 30)
N_WEEKS = 16
RACE_DATE = date(2026, 7, 19)  # Sunday of week 16

TARGET_MILES = [28, 32, 35, 26, 38, 42, 45, 36, 48, 52, 50, 56, 60, 40, 26, 12]
TARGET_WORKOUTS = [1, 1, 1, 1, 1, 1, 2, 1, 2, 2, 2, 2, 2, 1, 1, 0]
TARGET_LONG_RUN = [10, 11, 12, 9, 14, 16, 16, 13, 18, 19, 19, 20, 20, 14, 10, 3]
PHASES = ["base"] * 6 + ["sharpen"] * 4 + ["peak"] * 3 + ["taper"] * 2 + ["race"]

assert len(TARGET_MILES) == len(TARGET_WORKOUTS) == len(TARGET_LONG_RUN) == len(PHASES) == N_WEEKS

WORKOUT_ZONE_BY_PHASE = {
    "base": "threshold",
    "sharpen": "interval",
    "peak": "marathon",
    "taper": "marathon",
    "race": None,
}


def week_starts() -> list[date]:
    return [WEEK1_MONDAY + timedelta(weeks=i) for i in range(N_WEEKS)]


def build_weeks(target_miles: list[int], target_long_run: list[int]) -> list[plan.WeekInput]:
    weeks: list[plan.WeekInput] = []
    for i, ws in enumerate(week_starts()):
        weeks.append(plan.WeekInput(
            week_start=ws.isoformat(),
            target_miles=float(target_miles[i]),
            target_workouts=TARGET_WORKOUTS[i],
            phase=PHASES[i],
            target_long_run_miles=float(target_long_run[i]) if target_long_run[i] else None,
            note=None,
        ))
    return weeks


def build_days(target_miles: list[int], target_long_run: list[int]) -> list[plan.DayInput]:
    days: list[plan.DayInput] = []
    for i, ws in enumerate(week_starts()):
        miles = target_miles[i]
        workouts = TARGET_WORKOUTS[i]
        long_run = target_long_run[i]
        phase = PHASES[i]
        zone = WORKOUT_ZONE_BY_PHASE[phase]

        if phase == "race":
            for d in range(6):
                the_date = ws + timedelta(days=d)
                days.append(plan.DayInput(
                    date=the_date.isoformat(), slot="rest" if d == 4 else "easy",
                    title="Shakeout" if d != 4 else None,
                    target_miles=None if d == 4 else round(miles * 0.15, 1),
                ))
            race_day = ws + timedelta(days=6)
            days.append(plan.DayInput(
                date=race_day.isoformat(), slot="race", title="Goal race",
                target_miles=26.2,
            ))
            continue

        mon, tue, wed, thu, fri, sat, sun = (ws + timedelta(days=d) for d in range(7))

        days.append(plan.DayInput(
            date=mon.isoformat(), slot="easy", target_miles=round(miles * 0.12, 1),
            target=plan.DayTarget(zone_name="easy"),
        ))

        if workouts >= 1:
            reps: plan.DayTarget = {"zone_name": zone} if zone else {}
            if zone == "interval":
                reps = {"zone_name": "interval", "reps": 6, "rep_distance_m": 1000.0}
            elif zone == "threshold":
                reps = {"zone_name": "threshold", "reps": 1, "rep_distance_m": 6000.0}
            elif zone == "marathon":
                reps = {"zone_name": "marathon", "reps": 3, "rep_distance_m": 3218.0}
            title = {"interval": "6x1000m", "threshold": "Tempo", "marathon": "MP intervals"}.get(zone, "Workout")
            days.append(plan.DayInput(
                date=tue.isoformat(), slot="workout", title=title,
                target_miles=round(miles * 0.18, 1), target=reps,
            ))
        else:
            days.append(plan.DayInput(
                date=tue.isoformat(), slot="easy", target_miles=round(miles * 0.12, 1),
                target=plan.DayTarget(zone_name="easy"),
            ))

        days.append(plan.DayInput(
            date=wed.isoformat(), slot="easy", target_miles=round(miles * 0.14, 1),
            target=plan.DayTarget(zone_name="easy"),
        ))

        if workouts >= 2:
            days.append(plan.DayInput(
                date=thu.isoformat(), slot="workout", title="Threshold",
                target_miles=round(miles * 0.16, 1),
                target={"zone_name": "threshold", "reps": 1, "rep_distance_m": 4800.0},
            ))
        else:
            days.append(plan.DayInput(
                date=thu.isoformat(), slot="easy", target_miles=round(miles * 0.12, 1),
                target=plan.DayTarget(zone_name="easy"),
            ))

        days.append(plan.DayInput(date=fri.isoformat(), slot="rest"))

        days.append(plan.DayInput(
            date=sat.isoformat(), slot="easy", target_miles=round(miles * 0.10, 1),
            target=plan.DayTarget(zone_name="easy"),
        ))

        days.append(plan.DayInput(
            date=sun.isoformat(), slot="long", title="Long run",
            target_miles=float(long_run), target=plan.DayTarget(zone_name="easy"),
        ))

    return days


def main() -> None:
    conn = db.connect()
    db.init_db(conn)

    plan_id = plan.create_plan(
        conn, title="Twin Cities Marathon", race_date=RACE_DATE.isoformat(),
        distance_bucket="marathon", goal_time_s=3 * 3600 + 15 * 60,
    )
    print(f"created plan_id={plan_id}")

    v1_id = plan.add_version(
        conn, plan_id,
        weeks=build_weeks(TARGET_MILES, TARGET_LONG_RUN),
        days=build_days(TARGET_MILES, TARGET_LONG_RUN),
        note="Initial 16-week build for Twin Cities Marathon.",
        author="agent",
        created_at=datetime(2026, 3, 28, 14, 0, tzinfo=timezone.utc),
    )
    print(f"created v1 version_id={v1_id}")

    # Mid-plan revision so version history, the diff view, and contemporaneous
    # scoring (version_n_used flipping 1 -> 2) all have something to show.
    v2_miles = list(TARGET_MILES)
    v2_miles[12], v2_miles[13], v2_miles[14] = 52, 36, 24
    v2_long_run = list(TARGET_LONG_RUN)
    v2_long_run[12] = 18
    v2_id = plan.add_version(
        conn, plan_id,
        weeks=build_weeks(v2_miles, v2_long_run),
        days=build_days(v2_miles, v2_long_run),
        note="Cutting the true peak week after two weeks of elevated soreness.",
        author="agent",
        created_at=datetime(2026, 6, 18, 9, 30, tzinfo=timezone.utc),
    )
    print(f"created v2 version_id={v2_id}")

    log1 = plan.add_log_entry(
        conn, plan_id, log_date="2026-06-23", action="skipped",
        reason="sore right calf, skipped the Tuesday tempo",
    )
    print(f"created log entry: {log1}")

    counts = derive_all(conn)
    print(f"derive_all counts: {counts}")


if __name__ == "__main__":
    main()
