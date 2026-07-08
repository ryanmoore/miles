# miles

Strava activity sync and MCP server for querying running data.

> **CLAUDE.md philosophy:** Keep this file terse. Cover key architecture decisions, library choices, and non-obvious conventions — enough context for a fresh agent without bloating. Only reference specific code (functions, SQL, patterns) when it caused substantial stumbles, needed repeated emphasis, or is central enough that a fresh agent would otherwise miss it.

## Commands

```bash
uv run miles-auth   # one-time OAuth setup
uv run miles-sync   # sync activities from Strava (--full to ignore last sync date; --extra to backfill laps for all runs, resumable daily)
uv run miles-mcp    # start MCP server (stdio)
uv run miles-api    # start web UI on http://localhost:8000
uv run miles-derive # rebuild derived values (run_type_inferred, laps.lap_type); no API calls
uv run pyright      # type check
```

## Architecture

```
Strava API → miles-sync → SQLite (data/activities.db)
                               ↓
              miles-mcp  (MCP server over stdio, Claude Code integration)
              miles-api  (FastAPI on :8000 + vanilla JS UI: Races · Builds · Compare · Training · Years · Plan)
```

Key files:
- `miles/db.py` — schema, upsert, `ActivityRow` / `LapRow` TypedDicts, `WORKOUT_TYPE_MAP`, `EFFECTIVE_RUN_TYPE_SQL`
- `miles/races.py` — race-distance buckets (`classify_race_distance`), nominal distances, marathon bounds
- `miles/inference.py` — infers run_type for untagged (`workout_type = 0`) activities from name/distance/history; explicit Strava tags always win
- `miles/fitness.py` — dated fitness estimates (`estimate_fitness`, monthly `fitness_checkpoints` table): tier-1 recency-weighted races (Riegel), tier-2 workout work-laps anchored as 5K pace, tier-3 training-pace envelope floor; every estimate carries a confidence level
- `miles/periods.py` — splits history into training periods at 3+ inactive weeks
- `miles/builds.py` — race-anchored build detection (18-week cap, ramp floor, prior-race bound)
- `miles/distance_builds.py` — generalizes `/api/marathons`/`/api/marathon-weeks` to every race distance (5K/10K/Half/Marathon/50K/Other), each with its own build-window length; Monday-aligned like all build windows
- `miles/build_paces.py` — per-build 5K/LT/MP pace claims: classifies each work lap by its pace ratio against the fitness-estimate 5K baseline at build start, name/tag keyword overrides (MP, LT/tempo/threshold) win first
- `miles/fitness_api.py` — fitness-trend-full (all-distance pace projections per checkpoint) and fitness-evidence (recomputed evidence trail for one checkpoint) backing the Training page's Fitness chart
- `miles/derive.py` — `derive_all`: full recompute of all derived values + `meta` version stamp; runs at end of every sync, self-heals on version mismatch when tools connect. Derived values are rebuildable — raw synced rows are ground truth
- `miles/classifier.py` — keyword-based `workout_label` classifier (extend `WORKOUT_LABEL_PATTERNS` to add new types) and `classify_laps`, which types each lap as warmup/work/recovery/float/cooldown/steady via positional work-block detection (speed gap split + HR-guarded edge trim)
- `miles/mcp_server.py` — 26 MCP tools (table in README): training periods/consistency, race history/PRs/equivalents/splits, fitness estimates, workout laps, `run_sql` escape hatch, and the training-plan read/write tools (the only non-read tools; used by `/miles-plan`)
- `miles/plan.py` — training-plan ground truth: immutable versioned snapshots (`create_plan`/`add_version`, no UPDATE path), zone targets frozen at authoring via `estimate_fitness`, contemporaneous `current_version_for_week` (version 1 floor rule), race auto-complete on sync. Plan tables are athlete-authored — exempt from `derive_all` rebuilds
- `miles/plan_adherence.py` — derived week-level adherence (on/close/off bands, pattern-only flags — never single misses), scored against the version that governed each week; rebuilt by `derive_all` for active+completed plans
- `miles/plan_api.py` — `/api/plan`, `/api/plan-adherence`, `/api/plan-progression`, `/api/plan-retrospective`, versions/diff endpoints backing plan.html
- `miles/api.py` — FastAPI endpoints `/api/marathons`, `/api/marathon-weeks`, `/api/weekly-history`, `/api/build-detail`, `/api/activity-laps`, `/api/build-workout-groups`, `/api/fitness-trend`, `/api/races`, `/api/years`, `/api/hr-pace-heatmap`; mounts the `distance_builds.py` and `fitness_api.py` routers; also serves `miles/static/`
- `miles/static/` — vanilla JS, no framework. `races.html` is the landing page (`/` redirects here): Overview tab plus a per-distance tab (5K/10K/Half/Marathon/50K/Other) for each, each with a stat strip, 3-mode chart, and dense sortable tables. `builds.html` indexes every detected build; `build.html#{race-date}` is the drill-down (stat strip, weekly calendar, lap panel, workout-group comparisons; falls back to a fixed distance-bucket window for races without a detected build). `compare.html` is a build-vs-build workbench (max 4, per distance-bucket). `training.html` has a stat strip and a per-distance Fitness chart with hover/click-to-pin evidence. `years.html` is year-over-year volume plus an HR-vs-pace lap scatter (see below). `plan.html` is the training plan vs reality (stat strip, planned-vs-actual weekly chart with adherence bands + flag shading, weekly calendar, easy-HR/workout-pace progression charts, version history with diffs; renders a race retrospective for a completed plan, an empty state when no plan exists). `design-lab.html` + `static/lab/*.js` are a kept-around design playground, not a shipped page. `theme.css` (tokens + shared table/tab/nav classes) and `nav.js` (header nav: Races · Builds · Compare · Training · Years · Plan; `chartTheme()` for ECharts) are shared by every page; `charts.js` holds shared `fmt`/color/`staggerEndLabels`/`sparklineSVG`/`makeSortable` helpers
- `.claude/commands/miles.md` — `/miles` skill: the running-analyst persona (strictly descriptive, data-calibrated tone, tool routing)
- `.claude/commands/miles-plan.md` — `/miles-plan` skill: the planner persona — the only thing that prescribes; drafts/revises plans in conversation, writes only on explicit approval (design record: `adr/0001-training-plans-as-versioned-ground-truth.md`)
- `.claude/commands/marathon-analysis.md` — `/marathon-analysis` skill for guided training analysis
- `screenshot.py` — visual verification; launches system chromium directly and connects Playwright over CDP (see Development notes)

## Data model

`run_type` is derived from Strava's `workout_type` int at sync time: `easy`(0) `race`(1) `long_run`(2) `workout`(3). Set by the athlete in Strava; 0 means *unset*, and those rows get a `run_type_inferred` (see `inference.py`). Query the effective type with `EFFECTIVE_RUN_TYPE_SQL` (`db.py`) — explicit tags always win.

Marathon detection: `run_type = 'race'` AND `distance_m BETWEEN 42000 AND 43500`.

Race effort: `activities.race_effort` (`raced`/`hard`/`casual`) + `effort_ratio` — actual pace vs the fitness estimate as of the day before the race, HR-corroborated. Derived and rebuildable like all classification.

Build windows default to 12 weeks before race day. Weeks are Monday-aligned:
```python
race_week_monday = race_dt - timedelta(days=race_dt.weekday())
build_start = race_week_monday - timedelta(weeks=build_weeks)  # always a Monday
```
Week offset SQL — anchored to `build_start` so all differences are positive (CAST truncates toward zero correctly):
```sql
CAST((julianday(DATE(start_date)) - julianday(build_start)) / 7.0 AS INTEGER) - 12 AS week_offset
```
Range: `>= build_start AND <= race_date` (includes race day in week 0).

## Workout laps & classification

`miles-sync` lazily fetches laps for all activities with effective type `workout` or `race` and stores them in the `laps` table. Each subsequent sync picks up any new ones automatically.

`miles/classifier.py` assigns a `workout_label` to each workout activity based on name keywords (e.g. "LT", "MP Flux", "Tempo"). Many activities remain unlabeled — either generic Strava auto-names ("Afternoon Run", "Evening Run") or one-off names. This is expected; query by `name LIKE` or `run_sql` for those. Labeled workouts support the `get_workout_laps` MCP tool for cross-build comparisons.

Laps use the athlete's manual lap button or Garmin auto-lap (1-mile splits). Filter out trivial laps (`distance_m < LAP_MIN_DISTANCE_M` (200) or `moving_time_s < LAP_MIN_MOVING_TIME_S` (45), both in `classifier.py`) when analyzing rep data.

`classify_laps` (classifier.py) assigns per-lap types, persisted to `laps.lap_type` by the derive step (`derive.py` is its only caller; the lap MCP tools read the column); `compare_workouts_by_build` stats cover `work` laps only. Laps under the 200m/45s floor classify as `recovery` when sandwiched inside a detected work block, else stay null. Known limitations: sub-floor laps never classify as `work` (drops hill sprints / 150–200m reps as reps), and uphill reps invert the pace signal so they classify as `float`, not `work`.

### HR-vs-pace heatmap (years.html)

`/api/hr-pace-heatmap` returns one raw point per lap (year, HR bucket, pace bucket) rather than pre-aggregated cells, because the client needs to combine an arbitrary set of selected years — counts and the median line both have to be re-derived per selection, not summed/averaged from per-year aggregates. Bucket widths are 5bpm / 20s-per-mile; with ~600 total qualifying laps across ~9 years, finer buckets (originally tried at 2bpm/10s) left most cells with 0-1 laps, making the median line pure noise. The chart renders each bucket as a circle (not a heatmap rect) sized by count normalized *within its HR column* — global normalization made sparse HR buckets look uniformly faint regardless of their own internal distribution. The bucket range sent to ECharts must be a contiguous, evenly-spaced sequence (not just the distinct observed values) or the category-axis index math used to place the median line desyncs from the actual pixel grid.

## Development notes

`miles-api` runs uvicorn with `reload=True` — edits to `api.py` or any `static/*.html` take effect on browser refresh. Do not kill or restart the server.

`miles/static/theme.css` is the token source (colors, chart palette, type, shared table/tab/nav classes) for every static page — new pages link it rather than redefining styles. `nav.js` injects the shared header and exposes `chartTheme()` for ECharts pages.

ECharts chart container must be `<div>`, not `<canvas>` — ECharts manages its own canvas internally.

Each races.html distance tab's build chart has three modes (Fastest / Recent 3 / PR vs latest) toggling which builds are highlighted. Set `animation: false` on ECharts instances — draw animations cause screenshot artifacts.

**`screenshot.py`:** run with `uv run python screenshot.py` while `miles-api` is running (needs the `playwright` dev dependency and a system chromium). Browser automation here launches chromium as a subprocess and attaches over CDP — reuse that pattern rather than `p.chromium.launch()`.

## Eval harness

`eval_miles.py` runs a question through the `/miles` analyst persona and saves a full transcript to `eval_results/` (gitignored). Use it to catch regressions after changing MCP tools or the persona prompt.

```bash
uv run python eval_miles.py "question" --label my-label
```

Standard eval questions are tracked in `evals.local.md` (also gitignored).

## Type checking

All code must pass `uv run pyright` with zero errors. Follow these practices:

- Annotate all function parameters and return types.
- Use `X | None` instead of `Optional[X]`.
- Use `TypedDict` for structured dicts that cross function boundaries (see `ActivityRow` in `db.py`). Import it from `typing_extensions`, never `typing` — pydantic rejects `typing.TypedDict` in FastAPI response models on Python 3.11, and TypedDicts flow between modules, so this applies everywhere.
- Use `isinstance` for type narrowing, not `hasattr`.
- Avoid `Any`; if a library type is imprecise, use `cast` or a narrow `assert` at the boundary.
