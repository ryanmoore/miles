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
- **Compare puts two to four sides on one page.** A side is a plan version, a
  plan's draft, or the athlete's synced runs over a block of weeks; each may be
  limited to a Monday range. The first side is the baseline and must be a
  plan: every other side is compared to it, never to each other.
  `plan.html?plan=A[&draft=1][&weeks=FROM..TO]` is the baseline; each further
  side is one repeated `vs=<plan id|actual>[:draft][:FROM..TO]` (`actual` needs a range). The page reuses the calendar renderers, and the `compare_plans`
  tool takes the same sides as a list and returns the same comparison as data.
- **Alignment.** If every other side has a week in common with the baseline,
  weeks pair by date and a week only some sides have pairs with nothing for
  the rest. Otherwise all sides pair by position (Week 1 beside Week 1), each
  keeping its own dates. A week's mileage is its target (a range at its
  midpoint), or the synced miles for runs.
- **Shading is relative to the baseline.** Each non-baseline day is tinted by
  how far it differs from the baseline's same weekday; the baseline row is
  never shaded. A mileage gap of at most max(1 mi, 15% of the larger day) with
  matching run types is the same day; bigger gaps and changed or one-sided
  run sessions step up through light, medium and strong. A day that differs
  only in strength sessions (or titles) is the faintest level; the tool still
  lists it. The tint is `--series-7`, which no slot or run-type colour uses.
- **Summaries cover the shared weeks.** Per-side totals over different week
  counts mislead, so the table and the tool's summaries cover the weeks every
  plan side has (by date), or the first N weeks of the shortest plan (by
  position). A runs side is summarised over its own weeks within that set;
  the Weeks column keeps each side's full count. When the set is smaller than
  some plan side, a line above the table and `shared_weeks_note` say so.
- **`copy_weeks_to_draft` writes one plan's weeks into a draft.** It copies
  weeks and days (frozen targets as stored, same dates) from another plan's
  latest committed version or draft, replacing each copied week in the target,
  so a sketch can start as an exact copy of the plan it is compared with and an
  agreed sketch can go into a revision draft without re-typing. Goals and the
  race are not copied.
- **Draft write tools return a compact acknowledgement**, not the draft;
  `get_draft` and `get_training_plan` return it all, optionally limited by
  `from_week`/`to_week`.
