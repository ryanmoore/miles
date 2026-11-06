"""Seed several plans into a SCRATCH copy of the DB, for visually reviewing
plan.html's states: a past completed plan with two versions (the second moves
its race), a current plan whose race was last weekend and that includes this
week as its post-race week, a committed next plan with no race yet, and two
candidate drafts.

Requires a scratch copy of a DB that already holds a synced history; any
plans in it are replaced by the seeded ones, overlaid on those activities.

Usage:
    cp data/activities.db /tmp/plan_demo.db
    MILES_DB=/tmp/plan_demo.db uv run python scripts/seed_demo_plan.py
    MILES_DB=/tmp/plan_demo.db uv run uvicorn miles.api:app --port 8001

All dates are relative to today, so the roles stay put whenever it is run.

SAFETY: refuses to run unless MILES_DB is set and points somewhere other than
the repo's real data/activities.db.
"""

import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DB_ENV = os.environ.get("MILES_DB", "")
assert _DB_ENV, "refusing to run — set MILES_DB to a scratch copy of the DB"
assert Path(_DB_ENV).resolve() != (_REPO / "data" / "activities.db").resolve(), \
    "refusing to run against the real data/activities.db — use a scratch copy"

sys.path.insert(0, str(_REPO))

from miles import db, goals, notes, plan  # noqa: E402
from miles.derive import derive_all  # noqa: E402
from miles.fitness import MILE_M  # noqa: E402
from miles.periods import monday_of  # noqa: E402

N_WEEKS = 16
THIS_MONDAY = monday_of(date.today())
WEEK1_MONDAY = THIS_MONDAY - timedelta(weeks=N_WEEKS)
RACE_DATE = THIS_MONDAY - timedelta(days=1)  # last Sunday, the end of week 16

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


FUELING_DOC = """# Race-day fueling

Start fueling early and keep it steady; take water at every aid station.

| Time | Mile | Take | Carbs (g) | Notes |
|------|------|------|-----------|-------|
| 0:00 | 0 | Breakfast, 90 minutes before the start | 100 | Plain and familiar |
| 0:45 | 6 | Gel + water at the first aid station | 25 | Sip, do not gulp |
| 1:30 | 12 | Gel + sports drink at the second aid station | 45 | Half a bottle |
| 2:15 | 18 | Gel + water at the third aid station | 25 | Add salt if it is warm |
| 3:00 | 24 | Gel, then whatever the aid station has | 25 | Finish strong |

<b>Raw HTML</b> in a document shows as text.
"""

CARB_LOAD_DOC = """# Carb load

Aim for **8 g of carbs per kg** of body weight each day, mostly from low-fiber foods.

- Breakfast: oatmeal, banana, honey
- Dinner: pasta with a plain tomato sauce

Cut fiber and keep fluids up. Nothing new.
"""


# Runs per week of the demo marathon that carry a strides note, and how far each
# week's synced miles fall from its target.
STRIDE_RUNS = [2, 3, 2, 1, 1, 2, 3, 2, 2, 1, 1, 2, 2, 2, 2, 0]
MILES_FACTOR = [1.0, 0.95, 1.05, 0.9, 1.0, 1.05, 1.0, 0.98, 1.06, 1.0, 0.92, 1.04, 0.96, 1.0, 0.9, 1.0]
DEMO_ID_FLOOR = 9_000_000  # races and runs seeded here sit above every real activity id
DEMO_RUN_ID = 9_100_000
# 20 m segments: easy running, then six strides.
STRIDE_PARTS = [(250, 3.2)] + [p for _ in range(6) for p in ((6, 5.8), (24, 3.2))] + [(20, 3.2)]


def week_starts() -> list[date]:
    return [WEEK1_MONDAY + timedelta(weeks=i) for i in range(N_WEEKS)]


def build_weeks(days: list[plan.DayInput], target_long_run: list[int]) -> list[plan.WeekInput]:
    """Each week targets the sum of its mileage-slot days, which commit requires."""
    weeks: list[plan.WeekInput] = []
    for i, ws in enumerate(week_starts()):
        week_end = (ws + timedelta(days=6)).isoformat()
        week_miles = sum(
            d.get("target_miles") or 0.0 for d in days
            if d["slot"] in plan.MILEAGE_SLOTS and ws.isoformat() <= d["date"] <= week_end
        )
        weeks.append(plan.WeekInput(
            week_start=ws.isoformat(),
            target_miles=round(week_miles, 1),
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


def simple_plan(
    conn: sqlite3.Connection, title: str, first_monday: date, n_weeks: int,
    race: dict[str, object] | None = None, commit: bool = True,
) -> tuple[int, int]:
    """A plain build (Tuesday easy, Thursday workout, Sunday long) of n_weeks
    from first_monday. Returns (plan_id, the draft's version_id)."""
    plan_id, version_id = plan.start_plan_draft(conn, title=title, **(race or {}))  # type: ignore[arg-type]
    weeks: list[plan.WeekInput] = []
    days: list[plan.DayInput] = []
    for i in range(n_weeks):
        monday = first_monday + timedelta(weeks=i)
        miles = 20.0 + 2 * i
        weeks.append(plan.WeekInput(week_start=monday.isoformat(), target_miles=miles, target_workouts=1, phase="base"))
        for offset, slot, share in ((1, "easy", 0.25), (3, "workout", 0.25), (6, "long", 0.5)):
            days.append(plan.DayInput(date=(monday + timedelta(days=offset)).isoformat(), slot=slot, target_miles=miles * share))
    plan.upsert_draft_weeks(conn, version_id, weeks)
    plan.upsert_draft_days(conn, version_id, days)
    if commit:
        plan.commit_plan(conn, plan_id, note=f"Initial {title} plan.")
    return plan_id, version_id


def add_demo_runs(conn: sqlite3.Connection) -> list[tuple[int, int, str]]:
    """Synced runs for the demo marathon's weeks, scaled from each week's target:
    Tuesday easy, Thursday workout, Saturday easy, Sunday long (three shakeouts
    in race week). Returns (activity_id, week index, run type) in date order."""
    workout_types = {"easy": 0, "long_run": 2, "workout": 3}
    runs: list[tuple[int, int, str]] = []
    for i, week in enumerate(week_starts()):
        if PHASES[i] == "race":
            plan_runs = [(0, 0.15, "easy"), (1, 0.15, "easy"), (3, 0.15, "easy")]
        else:
            plan_runs = [(1, 0.2, "easy"), (3, 0.25, "workout" if TARGET_WORKOUTS[i] else "easy"), (5, 0.15, "easy"), (6, 0.4, "long_run")]
        for weekday, share, run_type in plan_runs:
            activity_id = DEMO_RUN_ID + len(runs)
            miles = TARGET_MILES[i] * MILES_FACTOR[i] * share
            conn.execute(
                "INSERT INTO activities (activity_id, name, sport_type, start_date, workout_type, run_type, distance_m, "
                "moving_time_s, synced_at) VALUES (?, ?, 'Run', ?, ?, ?, ?, ?, ?)",
                [activity_id, f"Demo {run_type.replace('_', ' ')}", f"{(week + timedelta(days=weekday)).isoformat()}T07:00:00",
                 workout_types[run_type], run_type, miles * MILE_M, round(miles * 540), datetime.now(timezone.utc).isoformat()],
            )
            runs.append((activity_id, i, run_type))
    conn.commit()
    return runs


def add_stride_segments(conn: sqlite3.Connection, activity_id: int, week: int) -> None:
    rows, idx = [], 0
    for count, speed in STRIDE_PARTS:
        for _ in range(count):
            rows.append((activity_id, idx, 20.0 * idx, 0, round(speed * 100), 140))
            idx += 1
    conn.execute(
        "INSERT INTO terrain_segment_dates (activity_id, start_date, segment_count) VALUES (?, ?, ?)",
        [activity_id, week_starts()[week].isoformat(), idx],
    )
    conn.executemany(
        "INSERT INTO terrain_segments (activity_id, idx, dist_m, grade_dpct, speed_cms, heartrate, is_walking, ele_source) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
        rows,
    )
    conn.commit()


def add_stride_notes(conn: sqlite3.Connection, runs: list[tuple[int, int, str]]) -> None:
    """Strides notes on STRIDE_RUNS[week] of each week's non-workout runs, plus two
    stream-detection cases: a workout whose note lists only its reps (the strides
    after it still count) and a run whose detection the athlete rejected."""
    strides = [{"type": "strides", "count": 6, "duration_s": 20, "source": "athlete"}]
    for week in range(N_WEEKS):
        week_runs = [(a, t) for a, w, t in runs if w == week]
        if week == 9:
            rejected = week_runs[2][0]
            add_stride_segments(conn, rejected, week)
            notes.set_run_note(conn, rejected, {"short": "Easy; the surges were traffic lights.", "elements": [{"type": "strides", "count": 0, "source": "athlete"}]})
        for activity_id, run_type in [r for r in week_runs if r[1] != "workout"][: STRIDE_RUNS[week]]:
            notes.set_run_note(conn, activity_id, {"short": "Easy with strides.", "elements": strides, "tags": ["strides"]})
    workout = next(a for a, w, t in runs if w == 7 and t == "workout")
    add_stride_segments(conn, workout, 7)
    notes.set_run_note(conn, workout, {"short": "Tempo, strides after.", "elements": [{"type": "reps", "count": 1, "source": "laps"}]})


def add_race_run(conn: sqlite3.Connection, activity_id: int, race_day: date, distance_m: float, moving_time_s: int) -> None:
    conn.execute(
        "INSERT INTO activities (activity_id, name, sport_type, start_date, workout_type, run_type, distance_m, moving_time_s, synced_at) "
        "VALUES (?, 'Demo race', 'Run', ?, 1, 'race', ?, ?, ?)",
        [activity_id, f"{race_day.isoformat()}T08:00:00", distance_m, moving_time_s, datetime.now(timezone.utc).isoformat()],
    )
    conn.commit()


def seed_past_plan(conn: sqlite3.Connection) -> None:
    """A 12-week 10K plan that ended the week before the current plan began; its
    second version moves the race a day and sets a goal."""
    first = WEEK1_MONDAY - timedelta(weeks=12)
    race_saturday = first + timedelta(weeks=10, days=5)
    plan_id, _ = simple_plan(conn, "Spring 10K", first, 12, race={"race_date": race_saturday.isoformat(), "distance_bucket": "10K"})
    version_id = plan.start_revision_draft(conn, plan_id)
    plan.set_draft_race(conn, version_id, (race_saturday + timedelta(days=1)).isoformat(), "10K", 45 * 60)
    plan.commit_plan(conn, plan_id, note="Race moved to Sunday; added a goal time.")
    add_race_run(conn, 9_000_001, race_saturday + timedelta(days=1), 10000.0, 46 * 60)


def demo_goals() -> list[dict[str, object]]:
    """The demo marathon's emphasis: strides twice a week (none in race week),
    eight weeks over 40 miles before the taper, and a steady average."""
    weeks = week_starts()
    return [
        {
            "key": "strides", "horizon": "weekly", "type": "element_sessions",
            "params": {"types": ["strides", "hill_sprints"]}, "target": 2, "flag": True,
            "overrides": {weeks[-1].isoformat(): 0},
            "why": "Keep leg speed ticking over while the mileage builds.",
        },
        {
            "key": "big weeks", "horizon": "cycle", "type": "weekly_miles", "params": {},
            "aggregate": "weeks_at_least", "threshold": 40, "target": 8, "to_week": weeks[12].isoformat(),
            "why": "Enough 40-mile weeks before the taper to carry the marathon.",
        },
        {
            "key": "average", "horizon": "cycle", "type": "weekly_miles", "params": {},
            "aggregate": "average", "target": 38, "why": "A steady average guards against one big week hiding a soft block.",
        },
    ]


def seed_current_plan(conn: sqlite3.Connection) -> None:
    plan_id, v1_id = plan.start_plan_draft(
        conn, title="Demo Marathon", race_date=RACE_DATE.isoformat(),
        distance_bucket="marathon", goal_time_s=3 * 3600 + 15 * 60,
    )
    days = build_days(TARGET_MILES, TARGET_LONG_RUN)
    plan.upsert_draft_days(conn, v1_id, days)
    plan.upsert_draft_weeks(conn, v1_id, build_weeks(days, TARGET_LONG_RUN))
    goals.set_draft_goals(conn, v1_id, demo_goals())
    plan.commit_plan(conn, plan_id, note="Initial 16-week build for the demo marathon.")
    # Version 1 governs from week 1 whatever its committed_at, so this date
    # only makes the history read as authored before the plan began.
    authored_at = (datetime.combine(WEEK1_MONDAY, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE plan_versions SET created_at = ?, committed_at = ? WHERE version_id = ?", [authored_at, authored_at, v1_id])
    conn.commit()
    print(f"created plan_id={plan_id} v1 version_id={v1_id}")

    # The post-race recovery week belongs to this plan: a revision adds it
    # and relaxes the goal.
    v2_id = plan.start_revision_draft(conn, plan_id)
    plan.set_draft_race(conn, v2_id, RACE_DATE.isoformat(), "marathon", 3 * 3600 + 10 * 60)
    plan.upsert_draft_weeks(conn, v2_id, [
        plan.WeekInput(week_start=THIS_MONDAY.isoformat(), target_miles=12.0, target_workouts=0, phase="base", note="Recovery after the race"),
    ])
    plan.upsert_draft_days(conn, v2_id, [
        plan.DayInput(date=(THIS_MONDAY + timedelta(days=d)).isoformat(), slot="easy", target_miles=4.0) for d in (1, 3, 5)
    ])
    plan.commit_plan(conn, plan_id, note="Added the recovery week and eased the goal time.")
    print(f"created v2 version_id={v2_id}")

    log1 = plan.add_log_entry(
        conn, plan_id, log_date=(WEEK1_MONDAY + timedelta(weeks=5, days=1)).isoformat(), action="skipped",
        reason="sore right calf, skipped the Tuesday tempo",
    )
    print(f"created log entry: {log1}")
    add_race_run(conn, 9_000_002, RACE_DATE, 42195.0, 3 * 3600 + 12 * 60)
    plan.auto_complete_plan(conn)

    notes.set_plan_doc(conn, plan_id, title="Race-day fueling", start_date=RACE_DATE.isoformat(), body=FUELING_DOC)
    notes.set_plan_doc(
        conn, plan_id, title="Carb load", body=CARB_LOAD_DOC,
        start_date=(RACE_DATE - timedelta(days=2)).isoformat(), end_date=(RACE_DATE - timedelta(days=1)).isoformat(),
    )


def seed(conn: sqlite3.Connection) -> None:
    db.init_db(conn)
    for table in ("plan_docs", "plan_goals", "plan_adherence", "plan_log", "plan_days", "plan_weeks", "plan_versions", "plans"):
        conn.execute(f"DELETE FROM {table}")
    for table in ("run_notes", "terrain_segments", "terrain_segment_dates", "activities"):
        conn.execute(f"DELETE FROM {table} WHERE activity_id >= {DEMO_ID_FLOOR}")
    seed_past_plan(conn)
    seed_current_plan(conn)
    add_stride_notes(conn, add_demo_runs(conn))
    # Next block: committed, race not chosen yet. Two candidates for the cycle after it.
    simple_plan(conn, "Next block", THIS_MONDAY + timedelta(weeks=1), 12)
    after = THIS_MONDAY + timedelta(weeks=13)
    simple_plan(conn, "Fall 50K", after, 12, race={"race_date": (after + timedelta(weeks=11, days=5)).isoformat(), "distance_bucket": "50K"}, commit=False)
    simple_plan(conn, "Spring 5K", after, 8, race={"race_date": (after + timedelta(weeks=7, days=5)).isoformat(), "distance_bucket": "5K"}, commit=False)

    counts = derive_all(conn)
    print(f"derive_all counts: {counts}")


def main() -> None:
    with closing(db.connect()) as conn:
        seed(conn)


if __name__ == "__main__":
    main()
