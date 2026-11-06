# ADR 0006 · Several plans over time: the race lives on the version

Status: accepted

## Context

ADR 0001 allowed one active plan, fixed each plan's race on the `plans` row,
and required the weeks to end at the race week. Real cycles overlap that
model: the athlete looks back at past cycles, drafts several candidates for the
next one before choosing a race, and may move the race date in a revision. The
week after a race belongs to either the cycle that just ended or the next one.

## Decision

- **The race is on the version and optional.** `plan_versions` carries
  `race_date`, `distance_bucket` and `goal_time_s` (nullable); `plans` keeps
  only the title and status. A plan's race is its latest committed version's
  (a never-committed plan: its draft's). A revision draft starts with the prior
  version's race and may change or clear it; version diffs report race changes.
- **No overlapping weeks replaces "one active plan".** A commit is rejected when
  its weeks overlap any week of another committed, non-abandoned plan. Weeks stay
  contiguous and, when a race is set, must include the race week; they may extend
  past it. Any number of drafts (candidates) may coexist.
- **Roles are derived from dates, never stored.** Status stays draft, active,
  completed or abandoned. Role is `current` (committed, weeks contain this
  week), `next` (starts later), `past` (weeks ended), `candidate` (never
  committed) or abandoned. A gap week has no current plan.
- **Whichever plan includes the post-race week owns it.** A completed plan can
  still be revised to add that week; weeks a revision adds rewrite no history, so
  past-week protection leaves them alone.
- Auto-complete checks each active plan against its own race. Adherence is
  scored per active or completed plan through its last week.

## Consequences

- `/api/plans` and `list_plans` list every plan and double as the cross-plan
  comparison.
- Supersedes ADR 0001's one-active-plan consequence and its race-week-ending rule.
