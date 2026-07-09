# ADR 0001 · Training plans as versioned ground truth, adherence as a derived layer

Status: accepted · 2026-07-07

## Context

miles stores raw synced Strava data as ground truth and rebuilds every
classification from it (`derive_all`). Training plans introduce the first
athlete-*authored* data: prescriptive targets that reality will be compared
against, drafted and revised in conversation via the `/miles-plan` skill.
Two failure modes drove the design: silently rewriting a plan after the fact
to make past training look adherent, and judging a week against targets the
athlete hadn't seen yet.

## Decision

- **Plans are ground truth, like activities — not derived.** The plan tables
  (`plans`, `plan_versions`, `plan_weeks`, `plan_days`, `plan_log`) are
  athlete-authored rows in `activities.db`, exempt from `derive_all` rebuilds.
  The only post-creation mutation anywhere is the `plans.status` flip
  (active → completed/abandoned).
- **Versions are immutable, append-only snapshots.** A revision writes a
  complete new copy of the week/day rows; no UPDATE path exists. Diffs are
  computed, never stored.
- **Weeks are judged against the contemporaneous version.** Week W scores
  against the latest version *committed* before W's Monday (not merely
  created — a draft revision edited over several days takes effect from its
  commit), so revisions take effect the following Monday and rewriting
  history is structurally impossible. Floor rule: version 1 governs from the
  plan's first week even though it was committed after that Monday.
- **The week is the contract; the day is a sketch.** Day rows exist for the
  calendar UI and workout targets, but scoring aggregates to the
  Monday-aligned week — a workout done Thursday instead of Tuesday scores
  identically. Day-level reality ("skipped Tue") goes in `plan_log`, which
  never bumps a version and never changes scoring.
- **Targets freeze at authoring.** Zone-anchored targets resolve to concrete
  pace ranges via a live `estimate_fitness` at version-creation time and are
  stored resolved; adherence reads only the frozen values. Targets never
  drift with the moving fitness estimate; a revision re-resolves.
- **Adherence is derived and rebuildable** (`plan_adherence`, rebuilt by
  `derive_all`, versioned by `DERIVE_VERSION`), scored for active *and*
  completed plans, restricted to weeks the sync has actually seen (below).
  Week bands: actual/target mileage 0.90–1.10 is "on", 0.80–1.15 bounds
  "close", outside is "off" — overshoot is scored symmetrically with
  undershoot (sustained overshoot is injury risk, not virtue). A range week
  (`target_miles`/`target_miles_hi`) scores actual against whichever bound is
  nearer — inside the range scores a flat 1.0; a week authored with neither
  bound is deliberately unspecified and is judged on workout count alone
  (mileage never worsens its band). A long-run target expressed in minutes
  is satisfied the same way as miles — any single run at 0.85x or more of
  the target, any day — and a week carrying both forms is satisfied by
  either. A day's `terrain='trail'` suppresses pace judgment entirely (grade
  makes road pace bands meaningless) but never suppresses volume or
  workout-count credit. Strength activities are counted into
  `actual_strength_days` and displayed, but never scored — no band, no
  flag — because gym logging in Strava is too inconsistent to judge silence.
  Flags fire on patterns only — 2+ consecutive qualifying weeks, never a
  single day or week — because ~90% adherence is a great outcome and
  day-level deviation is normal, not failure. Thresholds live as named
  module constants in `plan_adherence.py`.
- **Adherence never scores a week the sync hasn't fully covered.** A week
  whose Sunday falls on or after `meta.last_sync_at`'s date gets no
  `plan_adherence` row at all — absence, not a score — so a mid-week partial
  sync can never read as a bad week. A DB with no `last_sync_at` stamped yet
  (pre-v2) falls back to scoring every week that has already ended as of
  today, matching the original behavior.

## Consequences

- "What did the plan say at the time?" stays answerable forever; adherence
  history survives plan completion.
- Snapshot cost is trivial (~120 rows per version for a 16-week plan).
- One active plan at a time (enforced on create); abandon+create covers the
  edge cases until multiple concurrent plans earn their complexity.
- The UI and MCP tools read frozen targets and derived bands; nothing
  re-resolves targets at query time.
