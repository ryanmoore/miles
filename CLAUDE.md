# miles

Strava activity sync and MCP server for querying running data.

## Commands

```bash
uv run miles-auth   # one-time OAuth setup
uv run miles-sync   # sync activities from Strava (--full to ignore last sync date)
uv run miles-mcp    # start MCP server (stdio)
uv run miles-api    # start web UI on http://localhost:8000
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
- `miles/db.py` — schema, upsert, `ActivityRow` TypedDict, `WORKOUT_TYPE_MAP`
- `miles/mcp_server.py` — MCP tools: `get_weekly_mileage`, `get_activities`, `get_training_block`, `get_marathon_comparison`, `run_sql`
- `miles/api.py` — FastAPI endpoints `/api/marathons` and `/api/marathon-weeks`, also serves `miles/static/`
- `miles/static/index.html` — ECharts line chart + two comparison tables (vanilla JS, no framework)
- `.claude/commands/marathon-analysis.md` — `/marathon-analysis` skill for guided training analysis

## Data model

`run_type` is derived from Strava's `workout_type` int at sync time: `easy`(0) `race`(1) `long_run`(2) `workout`(3). Set by the athlete in Strava.

Marathon detection: `run_type = 'race'` AND `distance_m BETWEEN 42000 AND 43500`.

Build windows default to 12 weeks before race day. Week offsets align to race date:
`CAST((julianday(DATE(start_date)) - julianday(race_date)) / 7.0 AS INTEGER)`
— truncates toward zero; offset 0 = race week, -1 = week before, etc.

## Development notes

`miles-api` runs uvicorn with `reload=True` — edits to `api.py` or `static/index.html` take effect on browser refresh. Do not kill or restart the server.

ECharts chart container must be `<div>`, not `<canvas>` — ECharts manages its own canvas internally.

## Type checking

All code must pass `uv run pyright` with zero errors. Follow these practices:

- Annotate all function parameters and return types.
- Use `X | None` instead of `Optional[X]`.
- Use `TypedDict` for structured dicts that cross function boundaries (see `ActivityRow` in `db.py`).
- Use `isinstance` for type narrowing, not `hasattr`.
- Avoid `Any`; if a library type is imprecise, use `cast` or a narrow `assert` at the boundary.
