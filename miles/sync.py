import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.error
from collections import defaultdict
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from typing import cast

import click
from typing_extensions import TypedDict

from . import db, elevation, outbound, plan, strava_budget, strava_client, streams, terrain, terrain_backfill, weather as weather_module
from .classifier import classify_workout
from .derive import derive_all
from .errors import StravaAllowanceLowError, StravaNotConfiguredError, describe_error
from .races import run_sport_filter

_logger = logging.getLogger(__name__)

HISTORY_ENV = "MILES_SYNC_HISTORY"
DEFAULT_HISTORY_LIMIT = 900
# An incremental sync lists activities this far before the newest stored one.
INCREMENTAL_LOOKBACK = timedelta(days=30)

# meta keys written by every run, read by the health check and /metrics.
META_LAST_SUCCESS = "last_sync_at"
META_LAST_FAILURE = "last_sync_failed_at"
META_LAST_ERROR = "last_sync_error"
META_LAST_DURATION = "last_sync_duration_s"
META_LAST_NEW = "last_sync_new_activities"
# The last history pass's failure, cleared when a pass completes.
META_HISTORY_ERROR = "history_pass_error"


class SyncStatus(TypedDict):
    status: str  # "idle" | "running" | "done" | "error"
    phase: str | None
    done: int | None
    total: int | None
    until: str | None  # "HH:MM" local, set only during a "waiting" phase
    started_at: str | None
    finished_at: str | None
    error: str | None
    new_activities: int | None


def _idle_status() -> SyncStatus:
    return {
        "status": "idle", "phase": None, "done": None, "total": None, "until": None,
        "started_at": None, "finished_at": None, "error": None, "new_activities": None,
    }


class SyncProgress:
    """Thread-safe status for one sync run, polled by /api/sync/status and
    logged as it moves. Holds only the current phase -- there is no history,
    just "what's happening right now." One instance is reused across an
    entire run_sync call, including any waits on the rate limit."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _idle_status()

    def _update_locked(self, **changes: object) -> None:
        self._state = cast(SyncStatus, {**self._state, **changes})
        _log_progress(self._state)

    def _start_locked(self) -> None:
        self._state = {**_idle_status(), "status": "running", "started_at": datetime.now(timezone.utc).isoformat()}

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def try_start(self) -> bool:
        """Atomically switch to running unless a run is already in progress.
        Returns whether this call won the race and should launch the sync."""
        with self._lock:
            if self._state["status"] == "running":
                return False
            self._start_locked()
            return True

    def phase(
        self, name: str, *, done: int | None = None, total: int | None = None, until: str | None = None
    ) -> None:
        with self._lock:
            self._update_locked(phase=name, done=done, total=total, until=until)

    def finish(self, new_activities: int) -> None:
        with self._lock:
            self._update_locked(
                status="done", phase="done", new_activities=new_activities,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )

    def fail(self, error: str) -> None:
        with self._lock:
            self._update_locked(
                status="error", phase="error", error=error, finished_at=datetime.now(timezone.utc).isoformat()
            )

    def snapshot(self) -> SyncStatus:
        with self._lock:
            return cast(SyncStatus, dict(self._state))

    @property
    def status(self) -> str:
        with self._lock:
            return self._state["status"]


def _log_progress(snapshot: SyncStatus) -> None:
    phase = snapshot["phase"]
    if phase == "activities":
        _logger.info("  %s activities fetched...", snapshot["done"])
    elif phase == "waiting":
        _logger.info("\nRate limit hit — waiting until %s...", snapshot["until"])
    elif snapshot["total"] is not None and phase not in ("derive", "error"):
        _logger.info("  %s %s/%s...", phase, snapshot["done"], snapshot["total"])
    elif phase == "done":
        _logger.info("Sync complete. %s new/updated activities.", snapshot["new_activities"] or 0)


def history_enabled() -> bool:
    """Whether a sync also trickles in history. On unless MILES_SYNC_HISTORY
    is set to a false value."""
    return os.environ.get(HISTORY_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _phase_reporter(progress: SyncProgress, prefix: str = "") -> terrain_backfill.PhaseReporter:
    """Reports a pass's progress as sync phases named `<prefix><phase>`."""

    def report(phase: str, done: int, total: int) -> None:
        progress.phase(f"{prefix}{phase}", done=done, total=total)

    return report


def _fetch_laps(
    conn: sqlite3.Connection,
    ids: list[int],
    gate: strava_budget.Gate,
    report: terrain_backfill.PhaseReporter,
) -> int:
    """Fetch and store laps and the description for each id in order (one
    request each), stopping when the gate refuses. Each activity is stamped
    and committed on its own, so an interruption loses nothing. Returns how
    many were fetched."""
    if not ids:
        return 0
    total = len(ids)
    fetched = 0
    lap_total = 0
    report("laps", 0, total)
    try:
        for activity_id in ids:
            description, laps = gate.call("detail", lambda: strava_client.get_activity_detail(activity_id))
            db.set_activity_description(conn, activity_id, description)
            if laps is None:
                laps = gate.call("laps", lambda: strava_client.get_activity_laps(activity_id))
            db.store_activity_laps(conn, activity_id, laps)
            lap_total += len(laps)
            fetched += 1
            if fetched % 10 == 0 or fetched == total:
                report("laps", fetched, total)
    except StravaAllowanceLowError as e:
        # laps_synced_at stays unset for the rest, so a later run fetches them.
        _logger.warning("lap fetch stopped: %s", e)
    report("laps", fetched, total)
    _logger.info("Laps done. %s laps for %s activities.", lap_total, fetched)
    return fetched


def _pass_activities(
    conn: sqlite3.Connection, full: bool, progress: SyncProgress, gate: strava_budget.Gate
) -> tuple[int, set[int]]:
    """Pass 1: list activities newer than the last sync and store them. Returns
    how many of them were new or changed and the ids that were not in the
    database before."""
    after = None if full else db.last_synced_date(conn)
    if after:
        # activities.start_date is athlete-local wall time (db.py), but Strava's
        # `after` filter cuts on true UTC start_date, and local time can sit
        # hours either side of UTC: using it directly could put the cursor
        # later than the real cutoff and skip activities in the gap. The
        # lookback also catches uploads that arrive days after the run and
        # renames or retags of recent activities. Upserts are idempotent and
        # an unchanged listing leaves derived values alone.
        after = (datetime.fromisoformat(after) - INCREMENTAL_LOOKBACK).isoformat()
        _logger.info("Incremental sync: fetching activities after %s", after)
    else:
        _logger.info("Full sync: fetching all activities (may take a few minutes)...")

    def list_activities() -> list[db.ActivityRow]:
        listed: list[db.ActivityRow] = []
        progress.phase("activities", done=0)
        for i, row in enumerate(strava_client.get_activities(after_ts=after)):
            listed.append(row)
            if (i + 1) % 50 == 0:
                progress.phase("activities", done=i + 1)
        progress.phase("activities", done=len(listed))
        return listed

    # A 429 part-way through starts the listing over once the window opens.
    rows = gate.call(strava_budget.LISTING_KIND, list_activities)

    known = {r["activity_id"] for r in conn.execute("SELECT activity_id FROM activities").fetchall()}
    new_run_ids = {
        int(row["activity_id"])
        for row in rows
        if row["activity_id"] not in known and row["sport_type"] in terrain.TERRAIN_SPORT_TYPES
    }
    rewritten, changed_ids = db.upsert_activities(conn, rows)
    for activity_id in changed_ids:
        streams.discard_raw_streams(activity_id)

    total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    _logger.info("Done. %s new/updated. %s total in DB.", rewritten, total)
    return rewritten, new_run_ids


def _derive(conn: sqlite3.Connection, progress: SyncProgress) -> None:
    progress.phase("derive")
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    _logger.info("Derive done. %s", summary)


def _pass_details(conn: sqlite3.Connection, new_run_ids: Collection[int], gate: strava_budget.Gate, progress: SyncProgress) -> None:
    """Pass 2: laps for workouts and races that have none yet, then streams
    and terrain segments for the runs pass 1 found. Both stop at the Strava
    reserve; what is left is picked up by the next sync (laps) or the history
    pass (streams)."""
    # Backstamp anything already fetched by a prior sync so it's never refetched.
    conn.execute("""
        UPDATE activities SET laps_synced_at = datetime('now')
        WHERE laps_synced_at IS NULL
          AND activity_id IN (SELECT DISTINCT activity_id FROM laps)
    """)
    conn.commit()

    effective_run_type = db.effective_run_type_sql()
    unsynced = conn.execute(f"""
        SELECT activity_id, name FROM activities
        WHERE {effective_run_type} IN ('workout', 'race')
          AND laps_synced_at IS NULL
        ORDER BY start_date
    """).fetchall()
    if unsynced:
        _logger.info("Fetching laps for %s workout(s)...", len(unsynced))
        _fetch_laps(
            conn,
            [a["activity_id"] for a in unsynced],
            gate,
            _phase_reporter(progress),
        )

    # Label workouts and races whose laps are in but that have no label yet.
    unlabeled = conn.execute(f"""
        SELECT activity_id, name FROM activities
        WHERE {effective_run_type} IN ('workout', 'race') AND workout_label IS NULL
          AND laps_synced_at IS NOT NULL
    """).fetchall()
    for activity in unlabeled:
        unlabeled_name: str | None = activity["name"]
        if unlabeled_name:
            label = classify_workout(unlabeled_name)
            if label:
                conn.execute(
                    "UPDATE activities SET workout_label = ? WHERE activity_id = ?",
                    (label, activity["activity_id"]),
                )
    if unlabeled:
        conn.commit()

    if new_run_ids:
        _logger.info("Fetching streams for %s new run(s)...", len(new_run_ids))
        try:
            terrain_backfill.run_backfill(conn, _phase_reporter(progress), gate=gate, only_ids=new_run_ids)
        except terrain.TerrainRebuildingError:
            # Segmenting needs the terrain model, which a rebuild in progress
            # owns; the history pass picks these runs up afterwards.
            _note_terrain_rebuilding(progress)
        except (terrain_backfill.BackfillAbortedError, elevation.ElevationFetchError) as e:
            # The new activities are stored; the history pass retries what is
            # left, and its recorded failure reports a persistent one.
            _logger.warning("Stream fetch for new runs stopped: %s", describe_error(e, None))
        except Exception as e:
            # The terrain model is derived data: its failure must not skip
            # the weather, history, and derive passes or the last-sync stamp.
            _logger.warning("Terrain work for new runs failed: %s", describe_error(e, None), exc_info=True)
            conn.rollback()


def _note_terrain_rebuilding(progress: SyncProgress) -> None:
    _logger.info("Streams skipped: a terrain model rebuild is running; a later sync will fetch them.")
    progress.phase("streams-skipped-terrain-rebuilding")


def _pass_weather(conn: sqlite3.Connection, progress: SyncProgress) -> None:
    """Pass 3: weather for activities with a location and none yet. Open-Meteo,
    not Strava, so it never touches the Strava budget. The first failed request
    ends the pass, as does a location whose every window is refused; the
    activities it did not reach are picked up next sync. An activity the
    service had no hours for, or whose window was refused as unanswerable
    (status 400 or 422), keeps a value-less marker row and is asked about
    again only after NO_DATA_RETRY_DAYS."""
    retry_before = (datetime.now(timezone.utc) - timedelta(days=weather_module.NO_DATA_RETRY_DAYS)).isoformat()
    # Open-Meteo's hourly data is requested in UTC (weather.py's `timezone: UTC`
    # param), so this needs Strava's real UTC start_date — recovered from
    # raw_json, since activities.start_date now stores athlete-local wall time.
    needs_weather = conn.execute("""
        SELECT a.activity_id, a.start_lat, a.start_lng, a.moving_time_s,
               json_extract(a.raw_json, '$.start_date') AS utc_start_date
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.start_lat IS NOT NULL AND a.start_lng IS NOT NULL
          AND a.moving_time_s IS NOT NULL
          AND json_extract(a.raw_json, '$.start_date') IS NOT NULL
          AND (w.activity_id IS NULL
               OR (w.fetched_at < ? AND (w.hourly_json IS NULL OR (
                       w.temp_c_avg IS NULL AND w.temp_c_max IS NULL AND w.humidity_avg IS NULL
                       AND w.wind_kph_avg IS NULL AND w.precip_mm IS NULL
                       AND w.apparent_temp_c_max IS NULL))))
        ORDER BY a.start_date DESC
    """, [retry_before]).fetchall()
    if not needs_weather:
        return

    total_w = len(needs_weather)
    _logger.info("Fetching weather for %s activities...", total_w)

    # Group by rounded location (~11km grid); each group is then fetched in
    # windows of at most weather.MAX_WINDOW_DAYS days, one request per window.
    groups: defaultdict[tuple[float, float], list[weather_module.WeatherSpec]] = defaultdict(list)
    for act in needs_weather:
        key = (round(float(act["start_lat"]), 1), round(float(act["start_lng"]), 1))
        start_dt = datetime.fromisoformat(str(act["utc_start_date"]).replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        groups[key].append({
            "activity_id": act["activity_id"],
            "start_dt": start_dt,
            "duration_s": int(act["moving_time_s"]),
        })

    total_groups = len(groups)
    _logger.info("  %s location group(s).", total_groups)
    fetched_w = 0
    progress.phase("weather", done=0, total=total_groups)
    for g_idx, ((lat, lng), specs) in enumerate(groups.items(), 1):
        group_fetched = 0
        try:
            for rows, no_data in weather_module.fetch_weather_windows(specs, lat, lng):
                db.upsert_weather(conn, rows + no_data)
                group_fetched += len(rows)
        except (urllib.error.URLError, OSError, ValueError) as e:
            fetched_w += group_fetched
            _logger.warning(
                "Weather stopped: %s. %s new records; the rest wait for the next sync.", describe_error(e, None), fetched_w
            )
            return
        fetched_w += group_fetched
        _logger.info("  Group %s/%s (%.1f,%.1f): %s/%s fetched. Total: %s/%s", g_idx, total_groups, lat, lng, group_fetched, len(specs), fetched_w, total_w)
        progress.phase("weather", done=g_idx, total=total_groups)

    _logger.info("Weather done. %s new records.", fetched_w)


def remaining_lap_ids(conn: sqlite3.Connection) -> list[int]:
    """Runs whose laps were never fetched, long runs first then newest-first.
    A local query: a history pass with nothing left never touches the network."""
    effective_run_type = db.effective_run_type_sql()
    sport_clause, sport_params = run_sport_filter()
    return [
        row["activity_id"]
        for row in conn.execute(f"""
            SELECT activity_id FROM activities
            WHERE {sport_clause}
              AND laps_synced_at IS NULL
            ORDER BY CASE WHEN {effective_run_type} = 'long_run' THEN 0 ELSE 1 END,
                     start_date DESC
        """, sport_params).fetchall()
    ]


def _pass_history(conn: sqlite3.Connection, limit: int, gate: strava_budget.Gate, progress: SyncProgress) -> None:
    """Pass 4: the remaining laps, then the remaining streams and terrain
    segments, spending only what is left above the Strava reserve. A failure
    here is recorded under META_HISTORY_ERROR, not raised: the sync's own work
    is done."""
    lap_ids = remaining_lap_ids(conn)
    stream_work = terrain_backfill.refresh_backfill_queue(conn) > 0 or bool(terrain.activities_to_segment(conn))
    if not lap_ids and not stream_work:
        return

    report = _phase_reporter(progress, "backfill-")
    try:
        if lap_ids:
            _fetch_laps(conn, lap_ids[:limit], gate, report)
        if stream_work:
            try:
                terrain_backfill.run_backfill(conn, report, limit=limit, gate=gate)
            except terrain.TerrainRebuildingError:
                _note_terrain_rebuilding(progress)
        conn.execute("DELETE FROM meta WHERE key = ?", (META_HISTORY_ERROR,))
        conn.commit()
    except Exception as e:
        _logger.exception("History pass stopped: %s", describe_error(e, None))
        conn.rollback()
        _set_meta(conn, META_HISTORY_ERROR, describe_error(e))


def _run(
    conn: sqlite3.Connection,
    full: bool,
    history: bool,
    history_limit: int,
    reserve: int,
    progress: SyncProgress,
) -> int:
    """Run one sync in four passes. Returns the number of new/updated
    activities fetched.

      1. activities: list and store what is new (always runs, never gated)
      2. details: laps for new workouts, streams and terrain for new runs
      3. weather: Open-Meteo for activities without it
      4. history: remaining laps, streams and terrain (skipped when
         history=False)

    Passes 2 and 4 stop once the day's remaining Strava calls reach `reserve`.
    """
    gate = strava_budget.Gate(reserve, on_wait=lambda until: progress.phase("waiting", until=until))

    # Pass 1 fails the sync when Strava's daily limit is spent: without it
    # nothing newer is seen at all. The optional passes stop quietly instead.
    new_count, new_run_ids = _pass_activities(conn, full, progress, gate)

    # Derive here (not just at the end) so newly synced rows have
    # run_type_inferred populated before pass 2 queries effective type —
    # otherwise freshly-inferred races/workouts would be skipped for another
    # sync cycle.
    _derive(conn, progress)

    # Auto-complete each active plan whose own race (date +/-1 day, distance
    # bucket) now matches a synced effective-race activity — placed after the
    # derive above so a race that was only just inferred (run_type_inferred)
    # this same sync cycle is already visible to the match. The only mutation
    # is the status flip (see plan.auto_complete_plan).
    for completed_plan_id in plan.auto_complete_plan(conn):
        _logger.info("Plan %s auto-completed (matching race synced).", completed_plan_id)

    _pass_details(conn, new_run_ids, gate, progress)
    _pass_weather(conn, progress)
    if history:
        _pass_history(conn, history_limit, gate, progress)

    # Recompute all derived values (inferred run types, lap types, ...) from raw synced rows.
    _derive(conn, progress)

    # Stamp the moment this sync finished so readers (adherence, plan tools)
    # can tell how fresh the synced data is without guessing from activities.
    _set_meta(conn, META_LAST_SUCCESS, datetime.now(timezone.utc).isoformat())
    return new_count


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    db.set_meta(conn, key, value)
    conn.commit()


def run_sync(
    conn: sqlite3.Connection,
    full: bool,
    history: bool = True,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    reserve: int | None = None,
    progress: SyncProgress | None = None,
) -> int:
    """Run _run (every Strava request waits out a 429 or a nearly spent
    window through the gate, or stops the pass at the daily reserve). Shared
    by miles-sync (CLI) and /api/sync (web UI, which runs this on a
    background thread); returns the number of new/updated activities.
    Callers own the running/done/error transitions on `progress`
    (start/try_start, finish/fail) — this only reports phase updates during
    the run itself. The outcome (time, duration, new activities, or the
    failure) is recorded in `meta` for the health check and /metrics."""
    prog = progress if progress is not None else SyncProgress()
    started = time.monotonic()
    try:
        new_count = _run(
            conn, full, history, history_limit, strava_budget.default_reserve() if reserve is None else reserve, prog
        )
    except Exception as e:
        _set_meta(conn, META_LAST_FAILURE, datetime.now(timezone.utc).isoformat())
        _set_meta(conn, META_LAST_ERROR, describe_error(e))
        raise
    _set_meta(conn, META_LAST_DURATION, f"{time.monotonic() - started:.3f}")
    _set_meta(conn, META_LAST_NEW, str(new_count))
    return new_count


@click.command()
@click.option("--full", is_flag=True, help="Ignore last sync date and fetch everything.")
@click.option(
    "--history/--no-history", "history", default=history_enabled, show_default="on; $MILES_SYNC_HISTORY=0 turns it off",
    help="Also trickle in history: laps for older runs, then streams and terrain segments, newest first.",
)
@click.option(
    "--history-limit", "--extra-limit", "history_limit", type=int, default=DEFAULT_HISTORY_LIMIT,
    help="Max lap fetches and max stream fetches in the history pass of one sync.",
)
@click.option(
    "--reserve", "--extra-reserve", "reserve", type=int, default=None,
    help=f"Daily Strava calls to leave unused after the activity listing, so a later sync today can still fetch new "
         f"activities (default: ${strava_budget.RESERVE_ENV} or {strava_budget.DEFAULT_RESERVE}).",
)
@click.option("--extra", is_flag=True, hidden=True, help="Deprecated: the history pass runs by default.")
@click.option("--max-hr", type=int, default=None, help="Set max heart rate and exit (no Strava calls).")
@click.option("--long-run-floor", type=float, default=None, help="Set long-run distance floor in miles and exit (no Strava calls).")
def main(
    full: bool,
    history: bool,
    history_limit: int,
    reserve: int | None,
    extra: bool,
    max_hr: int | None,
    long_run_floor: float | None,
) -> None:
    # The run reports through logging; the terminal gets its plain messages.
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(console)
    _logger.setLevel(logging.INFO)
    conn = db.connect()
    db.init_db(conn)

    if max_hr is not None or long_run_floor is not None:
        existing = db.get_athlete(conn)
        merged_max_hr = max_hr if max_hr is not None else (existing["max_hr"] if existing else None)
        merged_floor = (
            long_run_floor if long_run_floor is not None
            else (existing["long_run_floor_miles"] if existing else None)
        )
        db.upsert_athlete(conn, max_hr=merged_max_hr, long_run_floor_miles=merged_floor)
        # A changed long_run_floor_miles is a global classifier input (inference.py),
        # not a raw-data change any writer marks dirty — needs the same full rebuild
        # as a DERIVE_VERSION bump to take effect on existing activities.
        counts = derive_all(conn, full=True)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
        _logger.info("Athlete profile updated. Derive done. %s", summary)
        return

    if db.get_athlete(conn) is None and sys.stdin.isatty():
        raw = click.prompt(
            "Max heart rate for HR-based analysis (Enter to skip)",
            default="", show_default=False,
        )
        prompted_max_hr: int | None
        try:
            prompted_max_hr = int(raw) if raw.strip() else None
        except ValueError:
            prompted_max_hr = None
        db.upsert_athlete(conn, max_hr=prompted_max_hr, long_run_floor_miles=None)

    outbound.claim()
    progress = SyncProgress()
    progress.start()
    try:
        new_count = run_sync(conn, full, history, history_limit, reserve, progress)
    except StravaNotConfiguredError as e:
        raise SystemExit(str(e)) from None
    progress.finish(new_count)


if __name__ == "__main__":
    main()
