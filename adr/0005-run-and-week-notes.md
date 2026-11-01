# ADR 0005 · Run and week notes as athlete-authored ground truth

Status: accepted

## Context

The record holds what a run measured, not what it was: strides never lapped,
a workout the lap classifier misread, how it felt, why it was cut short, a
week lost to illness or travel. Re-reading a run's meaning from its laps on
every query repeats the same misreadings, and the athlete's context has
nowhere to live.

## Decision

- **Notes are data, not derived.** `run_notes` (one row per activity) and
  `week_notes` (keyed by Monday) are the athlete's annotations: a short line
  per run, a long note for notable ones, the kind of run, elements (strides,
  hill sprints...), the athlete's quoted feel, tags, fueling, and how the run
  differed from the plan. Like the plan tables they are exempt from
  `derive_all`, and re-fetching an activity whose recording changed leaves its
  note alone (the words describe what the athlete did, not the old recording).
- **Overwrite in place.** A write changes the fields it names and stamps
  `updated_at`; there is no history table. `written_by` and `athlete_confirmed`
  say who wrote a note and whether the athlete agreed.
- **Detections are proposals.** Strides and hill sprints found in the 20 m
  stream segments (`elements.py`) are returned with `source: stream` and are
  never written to a note until the athlete confirms them.
- **Week notes record what life did; plan adjustments record what the plan
  changed.** They stay separate tables with separate tools.
- **One read for recent history.** `get_recent_history` folds notes, computed
  workout structure, stream detail, the athlete's Strava description, and the
  planned days of this week and next into one compact answer whose detail fades
  with age. A session is compared with the latest earlier one of similar
  structure, else the latest of its label.

## Consequences

- Notes are only as complete as their upkeep; `list_unnoted_runs` finds the
  gaps.
