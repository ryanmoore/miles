# ADR 0008 · The in-progress week is revisable

Status: accepted; amends ADR 0001's judging and revision-protection rules

## Context

ADR 0001 locked the in-progress week: a revision took effect the following
Monday. But the athlete revises mid-week exactly when reality changes (a hard
race, illness), and the week's remaining days are what the revision is about.

## Decision

- **Only ended weeks are protected.** At commit, a week whose Sunday is before
  today is overwritten with the version that governed it. The current week and
  later weeks take the draft as written.
- **Week W is judged by the latest version committed on or before W's Sunday**
  (committed_at's UTC date), with the version-1 floor unchanged. Calendar,
  adherence and weekly goals all read this through `current_version_for_week`.

## Consequences

- A revision committed after a week ended cannot change how it is judged, so
  history still cannot be rewritten after the fact.
- Adherence only scores weeks the sync has fully covered, and `commit_plan`
  re-runs the derive pass, so no score of the in-progress week can predate its
  revision.
