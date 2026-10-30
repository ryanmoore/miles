import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing_extensions import NotRequired, TypedDict

from .weather import WeatherRow

_logger = logging.getLogger(__name__)

# An upsert that flags more activities than this as changed (each loses its
# laps) is more likely a change in how values are formatted than real edits.
CHANGED_ACTIVITIES_WARN_COUNT = 50
# Smallest differences that count as an edit to a stored activity.
CHANGED_DISTANCE_M = 1.0
CHANGED_ELAPSED_S = 1.0


class LapRow(TypedDict):
    lap_id: int
    activity_id: int
    lap_index: int
    distance_m: float | None
    moving_time_s: int | None
    elapsed_time_s: int | None
    average_speed_mps: float | None
    average_heartrate: float | None
    max_heartrate: float | None
    average_cadence: float | None
    total_elevation_gain_m: float | None
    pace_zone: int | None
    raw_json: str


class ActivityRow(TypedDict):
    activity_id: int
    name: str | None
    description: NotRequired[str | None]
    sport_type: str
    # Athlete-local wall time, naive (no offset/Z suffix) — e.g. "2026-01-04T22:00:00",
    # not Strava's UTC start_date. Every day-bucketing consumer (DATE(start_date),
    # strftime(...)) depends on this being local, so "today" (date.today() in the
    # athlete's timezone) lines up with activities that actually happened that day.
    start_date: str | None
    workout_type: int
    run_type: str
    distance_m: float | None
    moving_time_s: int | None
    elapsed_time_s: int | None
    total_elevation_gain_m: float | None
    average_speed_mps: float | None
    max_speed_mps: float | None
    average_heartrate: float | None
    max_heartrate: float | None
    average_cadence: float | None
    gear_id: str | None
    strava_url: str
    synced_at: str
    start_lat: float | None
    start_lng: float | None
    raw_json: str


class AthleteRow(TypedDict):
    max_hr: int | None
    long_run_floor_miles: float | None
    updated_at: str | None


class PlanRow(TypedDict):
    plan_id: int
    title: str
    race_date: str
    distance_bucket: str
    goal_time_s: int | None
    status: str
    created_at: str


class PlanVersionRow(TypedDict):
    version_id: int
    plan_id: int
    version_n: int
    created_at: str
    note: str | None
    author: str
    # NULL = this is the mutable draft; a real timestamp = committed and
    # immutable. NotRequired so existing call sites that pre-date this column
    # (e.g. plan_api.py's hand-built rows) still type-check without it.
    committed_at: NotRequired[str | None]


class PlanWeekRow(TypedDict):
    version_id: int
    week_start: str
    target_miles: float | None  # range/point lo; NULL alongside target_miles_hi = deliberately unspecified
    target_miles_hi: float | None  # range upper bound; point week = lo (target_miles) only
    target_workouts: int
    target_long_run_miles: float | None
    target_long_run_minutes: float | None
    target_strength_days: int | None
    phase: str
    note: str | None


class PlanDayRow(TypedDict):
    version_id: int
    date: str
    seq: int
    slot: str
    title: str | None
    target_miles: float | None
    target_json: str | None  # JSON-encoded DayTarget (see plan.py), frozen at authoring
    terrain: str | None  # 'trail', or NULL meaning road (the default)
    note: str | None  # athlete-facing guidance, distinct from plan_log's reality-annotations
    target_minutes: float | None


class PlanLogRow(TypedDict):
    log_id: int
    plan_id: int
    date: str
    action: str
    reason: str | None
    created_at: str


class PlanAdherenceRow(TypedDict):
    """Derived table, rebuilt by derive_all; created empty alongside the
    ground-truth plan tables so run_sql always sees it."""
    plan_id: int
    week_start: str
    version_n_used: int | None
    actual_miles: float | None
    actual_workouts: int | None
    actual_strength_days: int | None  # displayed only — never bands or flags (gym logging is too inconsistent to judge silence)
    long_run_done: int | None
    mileage_ratio: float | None
    workout_pace_delta_s: float | None
    band: str | None
    flags_json: str | None


DB_PATH = Path(os.environ.get("MILES_DB", Path(__file__).parent.parent / "data" / "activities.db"))

WORKOUT_TYPE_MAP: dict[int, str] = {
    0: "easy",
    1: "race",
    2: "long_run",
    3: "workout",
    # Athlete-confirmed easy (not from Strava) — distinct from 0 so inference.py skips it.
    100: "easy",
}

def effective_run_type_sql(alias: str = "") -> str:
    """SQL expression for the effective run type, with column names optionally
    qualified by a table alias (e.g. "a"). Prefers the inferred label over Strava's
    default "easy" bucket, but only when the athlete never tagged the activity
    (workout_type == 0 is "unset"). Explicit athlete tags (workout_type 1-3, or 100
    for athlete-confirmed easy) always win.
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"COALESCE(CASE WHEN {prefix}workout_type = 0 THEN {prefix}run_type_inferred END, "
        f"{prefix}run_type)"
    )


def hr_trusted_sql(alias: str = "") -> str:
    """SQL condition that the athlete has not flagged the activity's heart
    rate as wrong (activities.hr_excluded_at, set from activity.html). Every
    reader that aggregates or derives from HR, the activity's own or its laps'
    or segments', filters on it or reads its HR through hr_sql; the
    activity's own page still shows what was recorded."""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}hr_excluded_at IS NULL"


def hr_sql(column: str, alias: str = "") -> str:
    """SQL expression for an HR column (e.g. "average_heartrate", or a laps
    or segments column with that table's alias) that is NULL when the
    activity's HR is flagged; `alias` qualifies the activities table."""
    return f"CASE WHEN {hr_trusted_sql(alias)} THEN {column} END"


def pace_sql(speed_expr: str) -> str:
    """SQL expression for min/mile from an m/s speed expression (a column or an
    aggregate of one); NULL when the speed is not positive."""
    return f"CASE WHEN {speed_expr} > 0 THEN ROUND(26.8224 / {speed_expr}, 2) ELSE NULL END"


def monday_sql(date_col: str) -> str:
    """SQL expression for the ISO date of the Monday on or before a date column."""
    return f"DATE({date_col}, '-' || ((CAST(strftime('%w', {date_col}) AS INTEGER) + 6) % 7) || ' days')"


EFFECTIVE_RUN_TYPE_SQL: str = effective_run_type_sql()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = DB_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")  # wait out another connection's write lock rather than raising immediately
    # WAL lets readers (queries, a backup) proceed during a write and a writer
    # during reads; the mode is stored in the file, so this is a no-op after
    # the first connection. It needs a local filesystem (shared-memory index).
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connection(opener: Callable[[], sqlite3.Connection] | None = None) -> Iterator[sqlite3.Connection]:
    """A connection from `opener` (default `connect`) that is closed on exit,
    including when the body raises."""
    conn = (opener or connect)()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_VERSION = 6


def add_missing_columns(conn: sqlite3.Connection, table: str, *columns: str) -> bool:
    """ALTER TABLE ... ADD COLUMN for each column definition whose name the
    table lacks. True when this call added any; a column another process
    added in the meantime is its to report, not ours."""
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    added = False
    for column in columns:
        if column.split()[0] in have:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        else:
            added = True
    return added


def init_db(conn: sqlite3.Connection) -> None:
    """Create or upgrade every table, once per process at startup. A database
    stamped with SCHEMA_VERSION (PRAGMA user_version) is left alone; changing
    any table or column definition means bumping SCHEMA_VERSION and adding
    the upgrade here so existing databases pick it up."""
    previous_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if previous_version >= SCHEMA_VERSION:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            activity_id   INTEGER PRIMARY KEY,
            name          TEXT,
            sport_type    TEXT,
            -- Athlete-local wall time, stored naive (no offset/Z suffix) — see
            -- ActivityRow.start_date in this module for why.
            start_date    TEXT,
            workout_type  INTEGER,
            run_type      TEXT,
            distance_m    REAL,
            moving_time_s INTEGER,
            elapsed_time_s INTEGER,
            total_elevation_gain_m REAL,
            average_speed_mps REAL,
            max_speed_mps     REAL,
            average_heartrate REAL,
            max_heartrate     REAL,
            average_cadence   REAL,
            gear_id       TEXT,
            strava_url    TEXT,
            synced_at     TEXT,
            start_lat     REAL,
            start_lng     REAL,
            raw_json      TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS laps (
            lap_id      INTEGER PRIMARY KEY,
            activity_id INTEGER NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
            lap_index   INTEGER NOT NULL,
            distance_m             REAL,
            moving_time_s          INTEGER,
            elapsed_time_s         INTEGER,
            average_speed_mps      REAL,
            average_heartrate      REAL,
            max_heartrate          REAL,
            average_cadence        REAL,
            total_elevation_gain_m REAL,
            pace_zone              INTEGER,
            raw_json               TEXT,
            UNIQUE(activity_id, lap_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            activity_id        INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            fetched_at         TEXT NOT NULL,
            temp_c_start       REAL,
            temp_c_end         REAL,
            temp_c_avg         REAL,
            temp_c_max         REAL,
            apparent_temp_c_max REAL,
            humidity_avg       REAL,
            precip_mm          REAL,
            wind_kph_avg       REAL,
            hourly_json        TEXT,
            raw_json           TEXT
        )
    """)
    add_missing_columns(
        conn,
        "activities",
        "workout_label TEXT",
        "start_lat REAL",
        "start_lng REAL",
        "raw_json TEXT",
        "run_type_inferred TEXT",
        "race_effort TEXT",
        "effort_ratio REAL",
        "dominant_intensity TEXT",
        "laps_synced_at TEXT",
        "description TEXT",
        "hr_excluded_at TEXT",
    )
    add_missing_columns(conn, "laps", "raw_json TEXT", "lap_type TEXT", "intensity TEXT", "elapsed_time_s INTEGER")
    add_missing_columns(conn, "weather", "raw_json TEXT")
    # The seed table holds the checkpoints that include casual-effort races (derive
    # pass 1) and is read only by pace-based race inference; fitness_checkpoints
    # is the final table every other reader uses.
    for checkpoint_table in ("fitness_checkpoints", "fitness_checkpoints_seed"):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {checkpoint_table} (
                month         TEXT PRIMARY KEY,
                confidence    TEXT,
                source_tier   INTEGER,
                pace_5k       REAL,
                pace_10k      REAL,
                pace_half     REAL,
                pace_marathon REAL
            )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # The start_date rewrite below once recorded itself under this key; the
    # schema version stamp replaced it.
    conn.execute("DELETE FROM meta WHERE key = 'start_date_local_migrated'")
    # activities.start_date is athlete-local wall time, stored naive (see
    # ActivityRow.start_date above). Rows synced before that held Strava's UTC
    # value; rewrite them from raw_json's start_date_local, stripping its
    # trailing 'Z' (Strava tags local wall time with a 'Z' suffix as if it
    # were UTC). Rows without raw_json or a start_date_local keep their value.
    # Re-running is a no-op on rows already rewritten.
    conn.execute("""
        UPDATE activities
        SET start_date = rtrim(json_extract(raw_json, '$.start_date_local'), 'Z')
        WHERE raw_json IS NOT NULL
          AND json_extract(raw_json, '$.start_date_local') IS NOT NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS athlete (
            id                   INTEGER PRIMARY KEY CHECK (id = 1),
            max_hr               INTEGER,
            long_run_floor_miles REAL,
            updated_at           TEXT
        )
    """)
    # Plans are athlete-authored ground truth (like activities), not derived —
    # exempt from derive_all. See adr/0001-training-plans-as-versioned-ground-truth.md.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plans (
            plan_id         INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            race_date       TEXT NOT NULL,
            distance_bucket TEXT NOT NULL,
            goal_time_s     INTEGER,
            status          TEXT NOT NULL DEFAULT 'active',
            created_at      TEXT NOT NULL
        )
    """)
    # Versions are immutable, append-only snapshots once committed — there is
    # no UPDATE path for a committed version. A version with committed_at NULL
    # is the one mutable draft (see plan.py); committing it stamps
    # committed_at in place rather than writing a new version_id. A revision
    # still writes a brand new (draft, then committed) version_id with a full
    # new set of weeks/days.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_versions (
            version_id   INTEGER PRIMARY KEY,
            plan_id      INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            version_n    INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            committed_at TEXT,
            note         TEXT,
            author       TEXT NOT NULL,
            UNIQUE(plan_id, version_n)
        )
    """)
    # Pre-v2 DBs have plan_versions without committed_at. Every version that
    # existed before the column was added was committed (no draft concept
    # existed), so backfill committed_at = created_at, only when the column is
    # being added: a real in-progress draft's committed_at is legitimately
    # NULL until commit_plan stamps it.
    if add_missing_columns(conn, "plan_versions", "committed_at TEXT"):
        conn.execute("UPDATE plan_versions SET committed_at = created_at WHERE committed_at IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_weeks (
            version_id              INTEGER NOT NULL REFERENCES plan_versions(version_id) ON DELETE CASCADE,
            week_start               TEXT NOT NULL,
            target_miles             REAL,
            target_miles_hi          REAL,
            target_workouts          INTEGER NOT NULL,
            target_long_run_miles    REAL,
            target_long_run_minutes  REAL,
            target_strength_days     INTEGER,
            phase                    TEXT NOT NULL,
            note                     TEXT,
            PRIMARY KEY (version_id, week_start)
        )
    """)
    # Migration: pre-v2 DBs have target_miles NOT NULL and lack the new range/
    # duration/strength columns. SQLite can't relax a NOT NULL constraint or
    # reorder columns via ALTER, so rebuild the table (create-new/copy/rename)
    # — guarded by checking target_miles's notnull flag, so this only runs
    # once per DB (a freshly created table above is already nullable and
    # skips straight past). Every existing row's committed weeks survive
    # unchanged; only the new columns are added, all NULL for old rows.
    week_cols = conn.execute("PRAGMA table_info(plan_weeks)").fetchall()
    if any(c["name"] == "target_miles" and c["notnull"] for c in week_cols):
        conn.execute("""
            CREATE TABLE plan_weeks_v2 (
                version_id              INTEGER NOT NULL REFERENCES plan_versions(version_id) ON DELETE CASCADE,
                week_start               TEXT NOT NULL,
                target_miles             REAL,
                target_miles_hi          REAL,
                target_workouts          INTEGER NOT NULL,
                target_long_run_miles    REAL,
                target_long_run_minutes  REAL,
                target_strength_days     INTEGER,
                phase                    TEXT NOT NULL,
                note                     TEXT,
                PRIMARY KEY (version_id, week_start)
            )
        """)
        conn.execute("""
            INSERT INTO plan_weeks_v2 (
                version_id, week_start, target_miles, target_workouts,
                target_long_run_miles, phase, note
            )
            SELECT version_id, week_start, target_miles, target_workouts,
                   target_long_run_miles, phase, note
            FROM plan_weeks
        """)
        conn.execute("DROP TABLE plan_weeks")
        conn.execute("ALTER TABLE plan_weeks_v2 RENAME TO plan_weeks")
    # seq exists so doubles are representable at the schema level (2+ = doubles);
    # v1 planners will rarely emit one.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_days (
            version_id     INTEGER NOT NULL REFERENCES plan_versions(version_id) ON DELETE CASCADE,
            date           TEXT NOT NULL,
            seq            INTEGER NOT NULL DEFAULT 1,
            slot           TEXT NOT NULL,
            title          TEXT,
            target_miles   REAL,
            target_json    TEXT,
            terrain        TEXT,
            note           TEXT,
            target_minutes REAL,
            PRIMARY KEY (version_id, date, seq)
        )
    """)
    add_missing_columns(conn, "plan_days", "terrain TEXT", "note TEXT", "target_minutes REAL")
    # "Life happened" annotations — day-level reality that never touches the plan
    # or bumps a version. No FK to a version; scoped to the plan only.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_log (
            log_id     INTEGER PRIMARY KEY,
            plan_id    INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            date       TEXT NOT NULL,
            action     TEXT NOT NULL,
            reason     TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Derived: rebuilt from scratch by derive_all, versioned by DERIVE_VERSION.
    # Created alongside the ground-truth tables so run_sql always sees it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_adherence (
            plan_id              INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            week_start           TEXT NOT NULL,
            version_n_used       INTEGER,
            actual_miles         REAL,
            actual_workouts      INTEGER,
            actual_strength_days INTEGER,
            long_run_done        INTEGER,
            mileage_ratio        REAL,
            workout_pace_delta_s REAL,
            band                 TEXT,
            flags_json           TEXT,
            PRIMARY KEY (plan_id, week_start)
        )
    """)
    add_missing_columns(conn, "plan_adherence", "actual_strength_days INTEGER")

    # Uploaded GPX bytes are ground truth (like activities' raw_json) --
    # everything else about a route is rebuildable from this blob. See
    # adr/0002-routes-raw-gpx-ground-truth.md.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpx_files (
            file_id     INTEGER PRIMARY KEY,
            sha256      TEXT NOT NULL UNIQUE,
            filename    TEXT NOT NULL,
            creator     TEXT,
            data        BLOB NOT NULL,
            uploaded_at TEXT NOT NULL
        )
    """)
    # One row per trk/rte found in a gpx_files blob. name/source/notes/
    # tags_json are athlete-authored (set at upload, changed via PATCH) and
    # are never touched by re-derivation; every other column below
    # derive_version is rebuilt from the raw bytes by routes.derive_route.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routes (
            route_id       INTEGER PRIMARY KEY,
            file_id        INTEGER NOT NULL REFERENCES gpx_files(file_id) ON DELETE CASCADE,
            track_index    INTEGER NOT NULL,
            kind           TEXT NOT NULL,
            gpx_name       TEXT,
            name           TEXT,
            source         TEXT,
            notes          TEXT,
            tags_json      TEXT NOT NULL DEFAULT '[]',
            created_at     TEXT NOT NULL,
            derive_version INTEGER,
            has_elevation  INTEGER,
            distance_m     REAL,
            gain_m         REAL,
            loss_m         REAL,
            min_ele_m      REAL,
            max_ele_m      REAL,
            ft_per_mi      REAL,
            bbox_south     REAL,
            bbox_west      REAL,
            bbox_north     REAL,
            bbox_east      REAL,
            start_lat      REAL,
            start_lng      REAL,
            end_lat        REAL,
            end_lng        REAL,
            shape          TEXT,
            UNIQUE(file_id, track_index)
        )
    """)
    # elevation_source is the athlete's preference (auto/file/dem); ele_source
    # is which one actually populated route_points.ele_m last derive
    # (file/dem/NULL); elevation_pending marks a 'dem'-eligible route whose
    # last drape attempt found tiles missing from the cache, so the next
    # derive or an explicit redrape should try again. See routes.py.
    add_missing_columns(
        conn,
        "routes",
        "elevation_source TEXT NOT NULL DEFAULT 'auto'",
        "ele_source TEXT",
        "elevation_pending INTEGER NOT NULL DEFAULT 0",
        "location TEXT",
        "location_source TEXT",
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_points (
            route_id INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            idx      INTEGER NOT NULL,
            lat      REAL NOT NULL,
            lng      REAL NOT NULL,
            dist_m   REAL NOT NULL,
            ele_m    REAL,
            PRIMARY KEY (route_id, idx)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_waypoints (
            waypoint_id INTEGER PRIMARY KEY,
            route_id    INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            name        TEXT,
            desc        TEXT,
            lat         REAL NOT NULL,
            lng         REAL NOT NULL,
            dist_m      REAL NOT NULL,
            offset_m    REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_climbs (
            route_id  INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            idx       INTEGER NOT NULL,
            kind      TEXT NOT NULL,
            start_m   REAL NOT NULL,
            end_m     REAL NOT NULL,
            gain_m    REAL NOT NULL,
            avg_grade REAL NOT NULL,
            PRIMARY KEY (route_id, idx)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_grade_bands (
            route_id INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            band     TEXT NOT NULL,
            share    REAL NOT NULL,
            PRIMARY KEY (route_id, band)
        )
    """)
    # A track the athlete deleted while other tracks of its file remain:
    # derivation recreates a route for every track in a stored file, and skips
    # these.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_tombstones (
            file_id     INTEGER NOT NULL,
            track_index INTEGER NOT NULL,
            PRIMARY KEY (file_id, track_index)
        )
    """)
    # Composition definition for a route generated by compose_route (source
    # 'composed' in routes.source); kind='connector' rows have no
    # leg_route_id (a road/trail bit we don't have a GPX for).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_legs (
            route_id     INTEGER NOT NULL REFERENCES routes(route_id) ON DELETE CASCADE,
            idx          INTEGER NOT NULL,
            kind         TEXT NOT NULL,
            leg_route_id INTEGER REFERENCES routes(route_id) ON DELETE SET NULL,
            from_m       REAL,
            to_m         REAL,
            distance_m   REAL,
            gain_m       REAL,
            note         TEXT,
            PRIMARY KEY (route_id, idx)
        )
    """)

    # These modules import this one (for connect()/DB_PATH), so importing
    # them at module level here would cycle; function-body imports break it.
    from .areas import init_area_schema
    from .candidate_sets import init_candidate_set_schema
    from .drive_starts import init_drive_schema
    from .events import init_event_schema
    from .extracts import init_extract_schema
    from .network import init_network_schema
    from .route_search import init_search_schema
    from .sections import init_section_schema
    from .terrain import init_terrain_schema
    init_event_schema(conn)
    init_terrain_schema(conn)
    init_extract_schema(conn)
    init_area_schema(conn)
    init_network_schema(conn)
    init_drive_schema(conn)
    init_search_schema(conn)
    init_candidate_set_schema(conn)
    init_section_schema(conn)
    # Graphs and sections built before the trail kind, bridge marks and
    # stretches carry none of these; their areas rebuild on the next
    # NETWORK_DERIVE_VERSION check. A judgment stored before sections had
    # levels was made on a whole section, which is a group now.
    add_missing_columns(conn, "net_edges", "trail_kind TEXT", "structure TEXT")
    add_missing_columns(conn, "net_sections", "level TEXT NOT NULL DEFAULT 'group'", "group_id INTEGER")
    add_missing_columns(conn, "net_section_judgments", "level TEXT NOT NULL DEFAULT 'group'")
    conn.execute("UPDATE net_section_judgments SET level = 'group' WHERE level = 'piece'")
    if previous_version == 0:
        _settle_routes_deleted_before_tombstones(conn)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _settle_routes_deleted_before_tombstones(conn: sqlite3.Connection) -> None:
    """Upgrade for databases written before route deletion was tombstoned.
    Deleting a route then removed only its routes row: its child rows stayed
    behind and its stored GPX file was re-derived into a new route. Every
    upload was derived immediately, so a track of a stored file that has no
    route was deleted; tombstone it, and drop the orphaned child rows.
    Runs only on the unversioned-to-versioned upgrade: later, a file with no
    route yet is just not derived yet."""
    from .gpx_parse import GpxParseError, parse_gpx_bytes

    for file_row in conn.execute("SELECT file_id, data FROM gpx_files").fetchall():
        file_id = file_row["file_id"]
        try:
            tracks = parse_gpx_bytes(bytes(file_row["data"]))["tracks"]
        except GpxParseError:
            continue
        have = {
            r["track_index"] for r in conn.execute("SELECT track_index FROM routes WHERE file_id = ?", [file_id]).fetchall()
        }
        for track in tracks:
            if track["track_index"] not in have:
                conn.execute(
                    "INSERT OR IGNORE INTO route_tombstones (file_id, track_index) VALUES (?, ?)",
                    [file_id, track["track_index"]],
                )
    for table in ("route_points", "route_waypoints", "route_climbs", "route_grade_bands", "route_legs"):
        conn.execute(f"DELETE FROM {table} WHERE route_id NOT IN (SELECT route_id FROM routes)")
    conn.execute(
        "UPDATE route_legs SET leg_route_id = NULL "
        "WHERE leg_route_id IS NOT NULL AND leg_route_id NOT IN (SELECT route_id FROM routes)"
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a meta row; the caller commits."""
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def mark_derive_dirty(conn: sqlite3.Connection, since_date: str) -> None:
    """Lower meta.derive_dirty_since to the earliest of any existing value and
    since_date (an ISO 'YYYY-MM-DD' string); never raises it. Every raw-data writer
    calls this so derive_all (derive.py) knows the earliest date whose derived values
    need recomputing. ISO date strings sort correctly as plain text, so MIN() needs
    no date parsing."""
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES ('derive_dirty_since', ?)
        ON CONFLICT(key) DO UPDATE SET value = MIN(value, excluded.value)
        """,
        [since_date],
    )


def upsert_activities(conn: sqlite3.Connection, rows: list[ActivityRow]) -> tuple[int, list[int]]:
    """Insert or refresh the Strava-owned columns. Columns the app derives or
    records itself (description, laps_synced_at, run_type_inferred, race and
    intensity labels) are never in the SET list, so re-syncing an activity
    does not wipe them. The exception is an activity whose distance, elapsed
    time or start changed on Strava (a trimmed or merged recording): its laps
    and description describe the old recording, so they are cleared and
    laps_synced_at reset for the next sync to fetch them again; its terrain
    segments and weather are dropped for the same reason.

    Returns how many rows were new or differed from what was stored, and the
    ids of the activities whose recording changed, so the caller can discard
    their cached raw streams once the transaction has committed."""
    rewritten = 0
    changed: list[int] = []
    dirty_dates: list[str] = []
    for row in rows:
        stored = conn.execute("SELECT * FROM activities WHERE activity_id = ?", [row["activity_id"]]).fetchone()
        if stored is not None and not any(stored[c] != row[c] for c in row if c not in _VOLATILE_COLUMNS):  # type: ignore[literal-required]
            continue
        rewritten += 1
        if row["start_date"]:
            dirty_dates.append(row["start_date"][:10])
        if stored is not None and _recording_changed(stored, row):
            changed.append(row["activity_id"])
            if stored["start_date"] and not _same_start(stored["start_date"], row["start_date"]):
                dirty_dates.append(stored["start_date"][:10])
    if len(changed) > CHANGED_ACTIVITIES_WARN_COUNT:
        _logger.warning(
            "%d of %d upserted activities differ from what is stored; their laps will be refetched",
            len(changed),
            len(rows),
        )
    conn.executemany("""
        INSERT INTO activities (
            activity_id, name, sport_type, start_date,
            workout_type, run_type,
            distance_m, moving_time_s, elapsed_time_s,
            total_elevation_gain_m,
            average_speed_mps, max_speed_mps,
            average_heartrate, max_heartrate, average_cadence,
            gear_id, strava_url, synced_at,
            start_lat, start_lng, raw_json
        ) VALUES (
            :activity_id, :name, :sport_type, :start_date,
            :workout_type, :run_type,
            :distance_m, :moving_time_s, :elapsed_time_s,
            :total_elevation_gain_m,
            :average_speed_mps, :max_speed_mps,
            :average_heartrate, :max_heartrate, :average_cadence,
            :gear_id, :strava_url, :synced_at,
            :start_lat, :start_lng, :raw_json
        )
        ON CONFLICT(activity_id) DO UPDATE SET
            name = excluded.name, sport_type = excluded.sport_type,
            start_date = excluded.start_date,
            workout_type = excluded.workout_type, run_type = excluded.run_type,
            distance_m = excluded.distance_m, moving_time_s = excluded.moving_time_s,
            elapsed_time_s = excluded.elapsed_time_s,
            total_elevation_gain_m = excluded.total_elevation_gain_m,
            average_speed_mps = excluded.average_speed_mps,
            max_speed_mps = excluded.max_speed_mps,
            average_heartrate = excluded.average_heartrate,
            max_heartrate = excluded.max_heartrate,
            average_cadence = excluded.average_cadence,
            gear_id = excluded.gear_id, strava_url = excluded.strava_url,
            synced_at = excluded.synced_at,
            start_lat = excluded.start_lat, start_lng = excluded.start_lng,
            raw_json = excluded.raw_json
    """, rows)
    if changed:
        placeholders = ",".join("?" for _ in changed)
        # Everything derived from the old recording goes, so the terrain
        # backfill queues the activity again and the weather pass refetches it.
        for table in ("terrain_segments", "terrain_segment_dates", "terrain_backfill_failures", "weather"):
            conn.execute(f"DELETE FROM {table} WHERE activity_id IN ({placeholders})", changed)
        conn.execute(f"DELETE FROM laps WHERE activity_id IN ({placeholders})", changed)
        conn.execute(
            f"UPDATE activities SET laps_synced_at = NULL, description = NULL WHERE activity_id IN ({placeholders})",
            changed,
        )
    if dirty_dates:
        mark_derive_dirty(conn, min(dirty_dates))
    conn.commit()
    return rewritten, changed


# Excluded from the comparison of a listed row with the stored one: the key,
# the sync stamp, and the raw payload, whose engagement counters move without
# the activity itself changing.
_VOLATILE_COLUMNS = frozenset({"activity_id", "synced_at", "raw_json"})


def _recording_changed(stored: sqlite3.Row, row: ActivityRow) -> bool:
    """Whether the listed row's distance, elapsed time or start differs from
    the stored activity's enough to be a different recording."""
    return (
        _differs(stored["distance_m"], row["distance_m"], CHANGED_DISTANCE_M)
        or _differs(stored["elapsed_time_s"], row["elapsed_time_s"], CHANGED_ELAPSED_S)
        or not _same_start(stored["start_date"], row["start_date"])
    )


def _differs(old: float | None, new: float | None, tolerance: float) -> bool:
    if old is None or new is None:
        return old is not new
    return abs(old - new) > tolerance


def _same_start(old: str | None, new: str | None) -> bool:
    """Whether two stored start_date strings are the same wall time. Compared
    as parsed datetimes so a change in how the string is written is not an
    edit; a string that does not parse is compared as text."""
    if old == new:
        return True
    if old is None or new is None:
        return False
    try:
        return datetime.fromisoformat(old) == datetime.fromisoformat(new)
    except ValueError:
        return False


def upsert_weather(conn: sqlite3.Connection, rows: list[WeatherRow]) -> None:
    conn.executemany("""
        INSERT OR REPLACE INTO weather (
            activity_id, fetched_at,
            temp_c_start, temp_c_end, temp_c_avg, temp_c_max,
            apparent_temp_c_max, humidity_avg, precip_mm, wind_kph_avg,
            hourly_json, raw_json
        ) VALUES (
            :activity_id, :fetched_at,
            :temp_c_start, :temp_c_end, :temp_c_avg, :temp_c_max,
            :apparent_temp_c_max, :humidity_avg, :precip_mm, :wind_kph_avg,
            :hourly_json, :raw_json
        )
    """, rows)
    conn.commit()


def upsert_laps(conn: sqlite3.Connection, laps: list[LapRow]) -> None:
    conn.executemany("""
        INSERT OR REPLACE INTO laps (
            lap_id, activity_id, lap_index,
            distance_m, moving_time_s, elapsed_time_s, average_speed_mps,
            average_heartrate, max_heartrate, average_cadence,
            total_elevation_gain_m, pace_zone, raw_json
        ) VALUES (
            :lap_id, :activity_id, :lap_index,
            :distance_m, :moving_time_s, :elapsed_time_s, :average_speed_mps,
            :average_heartrate, :max_heartrate, :average_cadence,
            :total_elevation_gain_m, :pace_zone, :raw_json
        )
    """, laps)
    activity_ids = sorted({lap["activity_id"] for lap in laps})
    if activity_ids:
        placeholders = ",".join("?" for _ in activity_ids)
        row = conn.execute(
            f"SELECT MIN(DATE(start_date)) AS d FROM activities WHERE activity_id IN ({placeholders})",
            activity_ids,
        ).fetchone()
        if row is not None and row["d"] is not None:
            mark_derive_dirty(conn, row["d"])
    conn.commit()


def store_activity_laps(conn: sqlite3.Connection, activity_id: int, laps: list[LapRow]) -> None:
    """Store a fetched activity's laps (there may be none) and stamp it so
    the laps are not fetched again."""
    if laps:
        upsert_laps(conn, laps)
    conn.execute("UPDATE activities SET laps_synced_at = datetime('now') WHERE activity_id = ?", (activity_id,))
    conn.commit()


def set_activity_description(conn: sqlite3.Connection, activity_id: int, description: str | None) -> None:
    conn.execute(
        "UPDATE activities SET description = ? WHERE activity_id = ?",
        [description, activity_id],
    )
    conn.commit()


def set_hr_excluded(conn: sqlite3.Connection, activity_id: int, excluded: bool) -> None:
    """Flag or unflag an activity's HR as wrong. Athlete-authored: sync's
    upsert never writes the column. The caller marks derive dirty and commits."""
    conn.execute(
        "UPDATE activities SET hr_excluded_at = CASE WHEN ? THEN COALESCE(hr_excluded_at, ?) END WHERE activity_id = ?",
        [excluded, datetime.now(timezone.utc).isoformat(timespec="seconds"), activity_id],
    )


def last_synced_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(start_date) FROM activities").fetchone()
    return row[0] if row and row[0] else None


def get_last_sync_at(conn: sqlite3.Connection) -> str | None:
    """UTC ISO timestamp of the end of the most recent successful miles-sync
    (meta.last_sync_at, stamped by sync.py), or None before the first sync
    since this column existed. Lets a reader answer "how stale is this data?"
    without guessing from activities.start_date, which says nothing once a
    sync completes with zero new activities."""
    return get_meta(conn, "last_sync_at")


def get_athlete(conn: sqlite3.Connection) -> AthleteRow | None:
    """Single-row athlete profile, or None if never set."""
    row = conn.execute(
        "SELECT max_hr, long_run_floor_miles, updated_at FROM athlete WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "max_hr": int(row["max_hr"]) if row["max_hr"] is not None else None,
        "long_run_floor_miles": float(row["long_run_floor_miles"]) if row["long_run_floor_miles"] is not None else None,
        "updated_at": row["updated_at"],
    }


def upsert_athlete(conn: sqlite3.Connection, *, max_hr: int | None, long_run_floor_miles: float | None) -> None:
    """Writes both fields as given (NULL means unset) plus updated_at. Callers wanting to
    preserve a field read it first via get_athlete and pass it back through."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO athlete (id, max_hr, long_run_floor_miles, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            max_hr = excluded.max_hr,
            long_run_floor_miles = excluded.long_run_floor_miles,
            updated_at = excluded.updated_at
        """,
        (max_hr, long_run_floor_miles, now),
    )
    conn.commit()
