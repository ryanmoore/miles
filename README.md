# miles

Local running training database built from Strava, queryable via Claude Code MCP.

## Quickstart

### 1. Get Strava API credentials

Go to [strava.com/settings/api](https://www.strava.com/settings/api) and create an app.
Set the callback domain to `localhost`.

Copy your Client ID and Client Secret into a `.env` file in this repo:

```
STRAVA_CLIENT_ID=123456
STRAVA_CLIENT_SECRET=abc...
```

### 2. Authorize

```bash
uv run miles-auth
```

Opens a browser, completes the OAuth flow, and writes `STRAVA_ACCESS_TOKEN` /
`STRAVA_REFRESH_TOKEN` into `.env`. If a key is missing or malformed, this prints exactly
which one and points back here instead of crashing.

### 3. Initial sync

```bash
uv run miles-sync
```

Downloads your full Strava history into `data/activities.db`. Two things to expect on a
first sync of a long history:

- Strava rate-limits at 15-minute windows. When hit, `miles-sync` prints
  `Rate limit hit — sleeping <n>s until next 15-min window...` and resumes automatically —
  this is normal, not a hang. A decade of daily running can take several windows back to back.
- Re-run the same command any time afterwards for an incremental sync (only new activities).
  Use `--full` to ignore the last sync date and re-fetch everything.
- Use `--extra` to gradually backfill laps for every run (not just workouts/races), most
  important first — resumable, so rerun daily until the queue is empty.
- The first interactive sync asks once for your max heart rate (Enter to skip). Set it later —
  or set a personal long-run distance floor — with `uv run miles-sync --max-hr 185` /
  `--long-run-floor 14` (updates the profile, rebuilds derived values, and exits; no Strava calls).

### 4. Hook the MCP server into Claude Code

```bash
claude mcp add miles -- uv run --directory /absolute/path/to/this/repo miles-mcp
```

`--directory` gives `uv` an absolute path so the server starts correctly regardless of
Claude Code's working directory. Reload your Claude Code session afterwards — the `miles`
MCP server is picked up automatically.

(A `.mcp.json.example` is also included if you prefer project-scoped config checked in
alongside the repo — copy it to `.mcp.json` and set `cwd` to this repo's absolute path.)

### 5. Try it

```
/miles
```

or ask directly:

- "What was my highest mileage week in 2023?"
- "Show me my workout pace trend over the last 6 months."
- "How did my HR compare across easy runs vs long runs in my last marathon build?"

## Multiple athletes on one machine

Each athlete needs their own `.env` (tokens) and their own database. Point a second copy
at a different DB file with `MILES_DB`:

```bash
MILES_DB=/path/to/friend.db uv run miles-sync
MILES_DB=/path/to/friend.db uv run miles-mcp
```

Defaults to `data/activities.db` in the repo when unset. When registering a second MCP
server via `claude mcp add`, pass it through with `-e`:

```bash
claude mcp add miles-friend -e MILES_DB=/path/to/friend.db -- uv run --directory /absolute/path/to/this/repo miles-mcp
```

## Web UI

```bash
uv run miles-api
```

Opens a local web interface at `http://localhost:8000` with five pages:

- **Races** — race history and PRs, with a build-comparison chart and dense per-build pace tables broken out by distance (5K/10K/Half/Marathon/50K/Other)
- **Builds** — every detected training build, one row per race, with shape sparklines and volume/workout stats
- **Compare** — put up to four builds (same distance bucket) side by side, chart and stat table
- **Training** — training periods, weekly volume with race markers, and a fitness-trend chart with evidence drill-down
- **Years** — year-by-year volume comparison

## MCP tools

| Tool | Description |
|---|---|
| `get_weekly_mileage` | Miles per ISO week, optional date range |
| `get_activities` | List runs filtered by effective run type, date range |
| `get_training_block` | Aggregate stats for a date range, broken down by run type |
| `get_training_periods` | Detected stretches of consistent training, their gaps, and race-anchored builds |
| `get_consistency_report` | Streaks, gaps, rolling volume and ramp — the "how consistent am I" tool |
| `get_race_history` | Every race at every distance with PR flags, effort labels, pre-race context |
| `get_personal_bests` | Per-distance PRs with full progression |
| `get_race_equivalents` | Riegel cross-distance equivalents; casual-effort races excluded by default |
| `get_race_splits` | Post-race split analysis (negative/even/positive verdict) |
| `get_race_comparison` | Any race's result alongside its pre-race training window |
| `get_marathon_comparison` | All marathon results with 12-week build breakdowns and peak week stats |
| `get_build_snapshot` | Week-by-week view of the build before any race |
| `get_fitness_estimate` | Estimated race paces and training zones as of a date, with confidence |
| `get_fitness_trend` | Monthly estimated-fitness checkpoints over time |
| `get_workout_laps` | Workout sessions with per-lap breakdown, filterable by label |
| `get_workout_session` | Lap-by-lap detail for one workout: lap types and intensity zones |
| `compare_workouts_by_build` | Cross-build comparison of a workout label's rep pace/HR |
| `get_easy_hr_trend` | Monthly easy-run HR and pace — long-term aerobic fitness signal |
| `get_activity_weather` | Hourly weather breakdown for one activity |
| `run_sql` | Ad-hoc read-only SQL against all tables, including derived columns |

`run_type` reflects the label you set in Strava (`easy`, `workout`, `long_run`, `race`);
untagged activities get an inferred type from name, distance, and pace history — your
explicit tags always win.
