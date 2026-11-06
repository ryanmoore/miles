# ADR 0007 · Per-cycle goals on the plan version

Status: accepted

## Context

Each training cycle has its own emphasis: strides a few sessions a week, a run of
high-mileage weeks, an average volume. The plan's fixed per-week columns (miles,
workouts, long run) could not express any of it, so a new emphasis meant a code
change.

## Decision

- **Goals are data on the version.** `plan_goals` rows belong to a plan version and
  are copied by a revision, like weeks. A week is judged by the goals of the version
  that governed it (the rule of ADR 0001); cycle goals are judged as the plan's latest
  version states them. A goal has a key, a required one-line `why`, a type with
  params, and a horizon.
- **A registry of types, not SQL.** Each type is a function from a plan week to a
  number (`goals.GOAL_TYPES`): run miles, runs with given elements, runs carrying a
  tag, runs of a given kind, days with a non-run activity. Adding a type is one
  function and one entry. `tagged_runs` is the escape hatch: anything recorded
  as a run-note tag becomes countable.
- **Weekly and cycle.** A weekly goal compares each week with a target (per-week
  overrides, e.g. 0 in race week) and may flag a pattern with the same 2+ consecutive
  scored weeks rule as adherence. A cycle goal rolls the weekly value up over the
  cycle or a span of it: weeks at or over a threshold, or the average, with progress
  and whether the target is still reachable. Comparison is always >=.
- **Computed on request, never for unsynced weeks.** Values are read from activities
  and notes when asked, nothing is stored or derived. A week the sync has not fully
  covered has no value, not zero (ADR 0001).
- **Strength days are a goal.** The weekly strength-days target and its actual count
  are gone; a migration turns each version's targets into an unflagged
  `activity_days` goal. Adherence never scored strength, and a goal does not either
  unless flagged.
- **A note's elements win per type.** For each run and element type, the run note's
  entry decides (count 0 rejects a false detection); only types the note does not list
  fall back to stream detections (ADR 0005). Strides done after a workout therefore
  count when the note lists only the reps.

## Consequences

- Judging a goal reads one number per week, so a new emphasis needs no schema change.
- Goals removed in a later version no longer show, though the weeks they governed
  were judged by them.
