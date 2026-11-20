import json

import pytest
from helpers import call_tool
from mcp.server.fastmcp import FastMCP

from miles import db, sync, sync_mcp


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    monkeypatch.setattr(sync, "PROGRESS", sync.SyncProgress())
    mcp = FastMCP("test")
    sync_mcp.register_sync_tools(mcp)
    return mcp


def test_sync_runs_without_history_and_reports_the_fresh_record(server: FastMCP, monkeypatch: pytest.MonkeyPatch) -> None:
    histories: list[bool] = []

    def fake_run_sync(conn, *, full, history, progress):
        histories.append(history)
        db.set_meta(conn, "last_sync_at", "2026-01-05T08:00:00+00:00")
        conn.commit()
        return 3

    monkeypatch.setattr(sync, "run_sync", fake_run_sync)

    result = call_tool(server, "sync_activities", {"wait_s": 10})

    assert histories == [False]
    assert result["started"] is True
    assert (result["status"], result["new_activities"]) == ("done", 3)
    assert result["last_sync_at"] == "2026-01-05T08:00:00+00:00"


def test_a_run_in_progress_is_joined_and_reported_when_the_wait_expires(server: FastMCP) -> None:
    sync.PROGRESS.try_start()
    sync.PROGRESS.phase("waiting", until="14:15")

    result = call_tool(server, "sync_activities", {"wait_s": 0})

    assert result["started"] is False
    assert (result["status"], result["phase"], result["until"]) == ("running", "waiting", "14:15")
