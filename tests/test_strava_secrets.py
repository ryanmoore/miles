"""Strava credentials must not reach anything that stores, returns, or logs
exception text, however the request fails.

Request libraries put the URL, query string included, in their transport
exceptions, and a library's own URL formatting can put the access token (and, on a
refresh, the client secret and refresh token) in the query. The fake session here fails the way
that looks in practice, with those values spelled out in the message.
"""

import logging
import sqlite3
import threading
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from helpers import FakeStravaSession, insert_activity

from miles import api, db, mcp_server, strava_budget, strava_client, sync
from miles.sync import SyncProgress

SECRET = "FAKE-SECRET-123"
REFRESH = "FAKE-REFRESH-456"
ACCESS = "FAKE-ACCESS-789"
SECRETS = (SECRET, REFRESH, ACCESS)


def _failing_session(fail_on: str) -> FakeStravaSession:
    """Fails the token request or the API call, naming the credentials in the
    message the way requests does."""
    return FakeStravaSession(
        {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 4_000_000_000},
        token_error=requests.exceptions.ProxyError(
            f"HTTPSConnectionPool(host='www.strava.com'): Max retries exceeded with url: "
            f"/oauth/token?client_id=1&client_secret={SECRET}&refresh_token={REFRESH}&grant_type=refresh_token"
        ) if fail_on == "token" else None,
        api_error=requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool(host='www.strava.com'): Max retries exceeded with url: "
            f"/api/v3/athlete/activities?access_token={ACCESS}&per_page=200"
        ),
    )


@pytest.fixture
def database() -> Path:
    conn = db.connect()
    insert_activity(conn, 1, start_date="2026-01-05T08:00:00")
    conn.close()
    return db.DB_PATH


@pytest.fixture(params=["token", "api"])
def failing_strava(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", REFRESH)
    monkeypatch.setattr(strava_client, "_http_session", lambda: _failing_session(request.param))
    monkeypatch.setattr(strava_client, "_api_session", lambda token: _failing_session(request.param))
    return request.param


def _every_stored_value(path: Path) -> str:
    conn = sqlite3.connect(path)
    parts: list[str] = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in conn.execute(f'SELECT * FROM "{table}"').fetchall():
            parts.extend(str(v) for v in row)
    conn.close()
    return "\n".join(parts)


def test_a_failing_strava_request_leaks_no_credential(
    database: Path, failing_strava: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    client = TestClient(api.app)

    sync.PROGRESS = SyncProgress()
    assert client.post("/api/sync").json() == {"status": "started"}
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(timeout=10)
    sync_status = client.get("/api/sync/status").json()
    assert sync_status["status"] == "error"
    assert "StravaUnavailableError" in sync_status["error"]

    conn = db.connect()
    sync._pass_history(conn, 10, strava_budget.Gate(0), SyncProgress())
    history_error = db.get_meta(conn, sync.META_HISTORY_ERROR)
    conn.close()
    assert history_error

    responses = [
        sync_status,
        history_error,
        client.get("/api/health", params={"verbose": 1}).json(),
        mcp_server.run_sql("SELECT key, value FROM meta"),
    ]
    surfaces = [str(r) for r in responses] + [_every_stored_value(database), caplog.text, capsys.readouterr().out]
    for surface in surfaces:
        for secret in SECRETS:
            assert secret not in surface
