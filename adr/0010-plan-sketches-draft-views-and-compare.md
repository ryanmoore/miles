# ADR 0010 · Sketches, viewable drafts, and plan compare

Status: accepted; ADR 0001's "plans are ground truth" is unaffected

## Context

An athlete often wants to see something before it is a plan: example weeks,
a rough block, a revision under discussion. A draft holds that,
but a revision draft had no page, and throwaway drafts piled up as candidates.

## Decision

- **A sketch is a never-committed plan with `plans.sketch_expires_at` set.**
  `start_plan_draft(sketch=true)` sets it to now + 14 days; every draft write
  pushes it out again. `plan_role` reports `sketch` instead of `candidate`.
- **Promotion is `commit_plan`**, which clears the flag. There is no other verb.
- **Pruning reuses `discard_draft`** and runs wherever plans are listed
  (`plan.list_plans`). Only rows with an expiry are pruned, so committed
  versions and ordinary drafts (candidates, revision drafts) never expire.
- **Drafts are viewable and linkable.** `/api/plan?draft=1` serves a plan's
  uncommitted version (404 when there is none) and `plan.html?plan=N&draft=1`
  renders it under a banner saying it is not committed (and, for a sketch,
  when it expires). Every draft tool returns that URL. Adherence, readiness,
  goals and the retrospective describe the committed plan, so a draft view
  omits them.
- **Compare puts two sides on one page.** A side is a plan version, a plan's
  draft, or the athlete's synced runs over a block of weeks; each may be
  limited to a Monday range. `plan.html?plan=A&vs=B|actual` (with `draft`,
  `weeks`, `vs_draft`, `vs_weeks`) reuses the calendar renderers, and the
  `compare_plans` tool returns the same comparison as data.
- **Alignment.** If the sides' week ranges overlap, weeks pair by date and a
  week only one side has pairs with nothing. Otherwise they pair by position
  (Week 1 beside Week 1), each side keeping its own dates. A week's mileage is
  its target (a range at its midpoint), or the synced miles for runs.
