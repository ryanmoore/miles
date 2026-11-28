# ADR 0011 · Supplementary sessions: checklists, check-offs and moves

Status: accepted

## Context

Athletes plan non-run sessions (strength, mobility, drills, any other practice)
beside their runs. Most are never Strava activities, so the record cannot say
whether one happened. They want each day to list what to do, to tick items off,
and to move a missed session to another day.

## Decision

- **One slot, tagged by kind.** `strength` is replaced by `supplementary`. The
  session's `kind` (free text: lowercase, short) and its checklist `items`
  (`{name, amount?}`, amount free text) live in `target_json`, so versioning and
  diffs treat them like any other target. The slot stays out of
  `MILEAGE_SLOTS` and adherence. A migration rewrites every `strength` day, in
  committed versions too, to `supplementary` with kind `strength`: a schema
  change, not a revision. The page colors each kind from a small palette by hash.
- **Reality is a log beside the plan (ADR 0001).** `plan_log` gains `seq` and
  `detail`; a row with `seq` is a structured, athlete-authored event on the
  planned session (plan, planned date, seq): `check` (item name, on/off), `done`,
  `skip`, `move` (target date). Events are append-only and the latest per subject
  wins, so a tick can be undone and a move reversed; moving back to the planned
  date clears it. Checks are stored by item name, so a revision that reorders or
  edits items keeps the ticks of the items it keeps. Plan versions and scoring
  never change, and the tables stay exempt from `derive_all`. A separate table
  was not needed: `plan_log` already is the athlete's day-level record, and
  free-text annotations (`seq` NULL) keep working beside the events.
- **Done is derived.** A session is done when every item is ticked, when it was
  marked done (the way to finish one with no items), or, for kind `strength`,
  when a strength activity (`db.STRENGTH_SPORT_TYPES`) exists on its effective
  date. Some items ticked is partial; a skip overrides everything. State is
  always judged on the effective date, the moved-to date or else the planned one.
- **Moves are unrestricted.** Any day to any day, past or future, even outside
  the plan's weeks. The calendar shows the session on its effective date with a
  ghost marker on the planned day. Today lists the sessions whose effective date
  is today, and recent unfinished ones with "Move to today" as the main action.
- **Goals read effective dates.** `supplementary_sessions` (params: `kind`,
  `partial`) counts, per plan week, the sessions of that kind done on a date in
  the week, taken from the plan's latest version.
- **Writes are unauthenticated** `POST /api/plans/{id}/sessions` and the
  `update_supplementary_session` MCP tool, like other athlete-authored writes.

## Consequences

- A session removed by a later revision keeps its events but is no longer shown.
- Goal values read sessions from the latest version, so a week's count follows
  the sessions that version still lists.
