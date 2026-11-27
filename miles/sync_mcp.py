"""MCP tool that refreshes the record from Strava."""

import json

import anyio
from mcp.server.fastmcp import FastMCP

from . import db, sync


def register_sync_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    async def sync_activities(wait_s: float = 60) -> str:
        """
        Pull new activities from Strava so the record is current before it is
        read. Cheap: one listing request, plus one or two per new workout or
        race and one per new run; no history backfill. Safe to call while a
        sync is already running (from the Sync button, the scheduler, or an
        earlier call): it joins that run rather than starting another. Waits
        up to `wait_s` seconds (default 60, max 300) until the record is
        current (activities, laps and derived values in place), not for the
        weather pass and later work, which continue in the background.

        Returns `started` (true if this call began the run, false if it
        joined one), `status` ("running", "done", "error"), `phase` (the
        current step; "waiting" means a Strava rate-limit window is open
        until `until`, HH:MM local, so the run is not stuck but will not
        finish soon), `done`/`total` progress in that phase, `error`,
        `record_current` (true once the record can be read, even while
        `status` is still "running"), `new_activities` (new or
        updated activities, once done), and `last_sync_at`/`days_stale` of
        the database. If `status` is "running" and `record_current` is false
        when the wait ends, call again to keep waiting.
        """
        started = sync.start_background(history=False)
        deadline = anyio.current_time() + min(wait_s, 300)
        while not sync.PROGRESS.snapshot()["record_current"] and sync.PROGRESS.status == "running" and anyio.current_time() < deadline:
            await anyio.sleep(0.25)
        snap = sync.PROGRESS.snapshot()
        with db.connection() as conn:
            last_sync_at = db.get_last_sync_at(conn)
        return json.dumps({**snap, "started": started, "last_sync_at": last_sync_at, "days_stale": db.days_stale(last_sync_at)})
