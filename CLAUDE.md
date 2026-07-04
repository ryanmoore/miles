# miles

Strava activity sync and MCP server for querying running data.

> **CLAUDE.md philosophy:** Keep this file terse. Cover key architecture decisions, library choices, and non-obvious conventions — enough context for a fresh agent without bloating. Only reference specific code (functions, SQL, patterns) when it caused substantial stumbles, needed repeated emphasis, or is central enough that a fresh agent would otherwise miss it.

## Commands

```bash
uv run miles-auth   # one-time OAuth setup
uv run miles-sync   # sync activities from Strava (--full to ignore last sync date)
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
              miles-api  (FastAPI on :8000 + vanilla JS UI)
```

Key files:
- `miles/db.py` — schema, upsert, `ActivityRow` / `LapRow` TypedDicts, `WORKOUT_TYPE_MAP`, `EFFECTIVE_RUN_TYPE_SQL`
- `miles/races.py` — race-distance buckets (`classify_race_distance`), nominal distances, marathon bounds
- `miles/inference.py` — infers run_type for untagged (`workout_type = 0`) activities from name/distance/history; explicit Strava tags always win
- `miles/derive.py` — `derive_all`: full recompute of all derived values + `meta` version stamp; runs at end of every sync, self-heals on version mismatch when tools connect. Derived values are rebuildable — raw synced rows are ground truth
- `miles/classifier.py` — keyword-based `workout_label` classifier (extend `WORKOUT_LABEL_PATTERNS` to add new types) and `classify_laps`, which types each lap as warmup/work/recovery/float/cooldown/steady via positional work-block detection (speed gap split + HR-guarded edge trim)
- `miles/mcp_server.py` — MCP tools: `get_weekly_mileage`, `get_activities`, `get_training_block`, `get_marathon_comparison`, `get_workout_laps`, `run_sql`
- `miles/api.py` — FastAPI endpoints `/api/marathons` and `/api/marathon-weeks`, also serves `miles/static/`
- `miles/static/index.html` — ECharts line chart (3 tabs: Fastest 5 / Recent 3 / PR vs Recent) + two comparison tables (vanilla JS, no framework)
- `.claude/commands/miles.md` — `/miles` skill: the running-analyst persona (strictly descriptive, data-calibrated tone, tool routing)
- `.claude/commands/marathon-analysis.md` — `/marathon-analysis` skill for guided training analysis
- `screenshot.py` — Playwright visual verification; uses system chromium (`/usr/bin/chromium-browser`)

## Data model

`run_type` is derived from Strava's `workout_type` int at sync time: `easy`(0) `race`(1) `long_run`(2) `workout`(3). Set by the athlete in Strava; 0 means *unset*, and those rows get a `run_type_inferred` (see `inference.py`). Query the effective type with `EFFECTIVE_RUN_TYPE_SQL` (`db.py`) — explicit tags always win.

Marathon detection: `run_type = 'race'` AND `distance_m BETWEEN 42000 AND 43500`.

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

`miles-sync` lazily fetches laps for all `run_type = 'workout'` activities and stores them in the `laps` table. Each subsequent sync picks up any new workouts automatically.

`miles/classifier.py` assigns a `workout_label` to each workout activity based on name keywords (e.g. "LT", "MP Flux", "Tempo"). Many activities remain unlabeled — either generic Strava auto-names ("Afternoon Run", "Evening Run") or one-off names. This is expected; query by `name LIKE` or `run_sql` for those. Labeled workouts support the `get_workout_laps` MCP tool for cross-build comparisons.

Laps use the athlete's manual lap button or Garmin auto-lap (1-mile splits). Filter out artifact laps (`moving_time_s < 30` or `distance_m < 0.02`) when analyzing rep data.

`classify_laps` (classifier.py) assigns per-lap types, persisted to `laps.lap_type` by the derive step (`derive.py` is its only caller; the lap MCP tools read the column); `compare_workouts_by_build` stats cover `work` laps only. Known limitations: laps under the 200m/45s floor are never classified (drops hill sprints / 150–200m reps entirely), and uphill reps invert the pace signal so they classify as `float`, not `work`.

## Development notes

`miles-api` runs uvicorn with `reload=True` — edits to `api.py` or `static/index.html` take effect on browser refresh. Do not kill or restart the server.

`miles/static/theme.css` is the token source (colors, chart palette, type, shared table/tab/nav classes) for every static page — new pages link it rather than redefining styles. `nav.js` injects the shared header and exposes `chartTheme()` for ECharts pages.

ECharts chart container must be `<div>`, not `<canvas>` — ECharts manages its own canvas internally.

The chart has three tabs (Fastest 5 / Recent 3 / PR vs Recent) toggling which builds are highlighted. Set `animation: false` on the ECharts instance — draw animations cause screenshot artifacts.

**`screenshot.py`:** requires `uv add --dev playwright` + `sudo apt install -y chromium-browser`. Run with `uv run python screenshot.py` while `miles-api` is running.

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
- Use `TypedDict` for structured dicts that cross function boundaries (see `ActivityRow` in `db.py`).
- Use `isinstance` for type narrowing, not `hasattr`.
- Avoid `Any`; if a library type is imprecise, use `cast` or a narrow `assert` at the boundary.
