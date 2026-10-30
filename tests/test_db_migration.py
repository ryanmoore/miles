import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from miles import db


def _insert_dirty_row(conn: sqlite3.Connection) -> None:
    """Insert a row shaped like one synced before the local-wall-time fix: start_date
    holds Strava's UTC value, and raw_json carries the true start_date_local (with its
    fake trailing 'Z') the migration should read from instead."""
    conn.execute(
        """
        INSERT INTO activities (activity_id, name, sport_type, start_date, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            1001,
            "Evening Run",
            "Run",
            "2026-01-05T04:00:00+00:00",
            json.dumps({"start_date": "2026-01-05T04:00:00Z", "start_date_local": "2026-01-04T22:00:00Z"}),
        ),
    )
    conn.commit()


def _start_date(conn: sqlite3.Connection, activity_id: int = 1001) -> str:
    return conn.execute("SELECT start_date FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()["start_date"]


def test_backfill_rewrites_start_date_to_local_wall_time_once_and_leaves_rows_without_raw_json(conn: sqlite3.Connection):
    _insert_dirty_row(conn)
    conn.execute("PRAGMA user_version = 0")  # an existing DB that predates the stamp

    db.init_db(conn)
    first = _start_date(conn)

    conn.execute("PRAGMA user_version = 0")
    db.init_db(conn)

    assert first == _start_date(conn) == "2026-01-04T22:00:00"

    conn.execute(
        "INSERT INTO activities (activity_id, name, sport_type, start_date, raw_json) VALUES (1, 'No raw json', 'Run', ?, NULL)",
        ("2026-01-05T04:00:00+00:00",),
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    db.init_db(conn)
    assert _start_date(conn, 1) == "2026-01-05T04:00:00+00:00"  # a row without raw json is left alone

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.execute("UPDATE activities SET start_date = '2026-01-05T04:00:00+00:00' WHERE activity_id = 1001")
    conn.commit()
    db.init_db(conn)  # stamped: the start_date rewrite does not run again
    assert _start_date(conn) == "2026-01-05T04:00:00+00:00"


# The schema a fresh database gets, by version. A change to any table or
# column definition changes the hash; the version must then be bumped, the
# upgrade for existing databases written in init_db, and the new hash recorded.
_SCHEMA_HASHES = {
    1: "aea5ae73e40fa9e1797a71628ba98d85203433a908c53572313b52db2ef83721",
    2: "026d105719904fe53a70183f867fca94de39b6f86128bb8fce245dda37931648",
    3: "261b879550f49105a9791fb512e2a6e0b5000ae14353509b1f457083923ceef4",
    4: "3cd68e80241e18807b486e21dee57602c2414598d2e7396143cb5fc1379f813a",
    5: "eced04bc3140de98412c8305f094cd28947afd1eb49c85b278c08b5cf1b8269d",
    6: "6c2443c41b2c620905daaaca78298770f2776ec2dcb110db19e0a9d83a929d19",
}


def _schema_hash(conn: sqlite3.Connection) -> str:
    """Comments and whitespace inside a CREATE statement are stored verbatim
    by SQLite and must not count as a schema change."""
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    normalized = [(r["type"], r["name"], " ".join(re.sub(r"--[^\n]*", "", r["sql"] or "").split())) for r in rows]
    return hashlib.sha256(json.dumps(normalized).encode()).hexdigest()


def test_the_schema_version_is_bumped_whenever_the_schema_changes(conn: sqlite3.Connection):
    recorded = _SCHEMA_HASHES.get(db.SCHEMA_VERSION)
    assert recorded is not None, f"record the schema hash for version {db.SCHEMA_VERSION}: {_schema_hash(conn)}"
    assert _schema_hash(conn) == recorded, (
        f"the schema changed without a version bump: bump SCHEMA_VERSION and add the upgrade "
        f"(then record the hash {_schema_hash(conn)} for the new version)"
    )


_DEPLOYED_TABLES = {
    "activities", "laps", "weather", "fitness_checkpoints", "meta", "athlete", "plans", "plan_versions",
    "plan_weeks", "plan_days", "plan_log", "plan_adherence", "gpx_files", "routes", "route_points",
    "route_waypoints", "route_climbs", "route_grade_bands", "route_legs",
}
_ADDED_ROUTE_COLUMNS = ("elevation_source", "ele_source", "elevation_pending", "location", "location_source")


def _layout(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name")]
    return {name: [tuple(r) for r in conn.execute(f"PRAGMA table_info({name})")] for name in names}


def _release_database(path: Path) -> sqlite3.Connection:
    """A database as the first release left it: only its tables, the routes
    table without the elevation columns, and no version stamp."""
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    db.init_db(old)
    for row in old.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
        if row["name"] not in _DEPLOYED_TABLES:
            old.execute(f"DROP TABLE {row['name']}")
    for column in _ADDED_ROUTE_COLUMNS:
        old.execute(f"ALTER TABLE routes DROP COLUMN {column}")
    old.execute("PRAGMA user_version = 0")
    old.commit()
    return old


def test_a_database_from_the_first_release_upgrades_to_the_schema_a_fresh_one_gets(conn: sqlite3.Connection, tmp_path: Path):
    old = _release_database(tmp_path / "release.db")
    assert "elevation_source" not in {r["name"] for r in old.execute("PRAGMA table_info(routes)")}
    old.execute("INSERT INTO plans (plan_id, title, race_date, distance_bucket, created_at) VALUES (1, 'P', '2026-05-30', '10K', '2026-01-01')")
    old.execute("INSERT INTO plan_versions (version_id, plan_id, version_n, created_at, committed_at, author) VALUES (1, 1, 1, '2026-01-02', NULL, 'agent')")
    old.execute("INSERT INTO meta (key, value) VALUES ('start_date_local_migrated', '1')")
    _insert_dirty_row(old)

    db.init_db(old)

    assert _layout(old) == _layout(conn)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_meta(old, "start_date_local_migrated") is None  # the retired marker is cleared
    assert _start_date(old) == "2026-01-04T22:00:00"
    assert old.execute("SELECT committed_at FROM plan_versions").fetchone()["committed_at"] is None  # a draft stays a draft
    assert old.execute("SELECT elevation_source FROM routes").fetchall() == []


def test_a_version_1_database_upgrades_to_the_schema_a_fresh_one_gets(conn: sqlite3.Connection, tmp_path: Path):
    old = _release_database(tmp_path / "v1.db")
    old.execute("ALTER TABLE routes ADD COLUMN elevation_source TEXT NOT NULL DEFAULT 'auto'")
    old.execute("ALTER TABLE routes ADD COLUMN ele_source TEXT")
    old.execute("ALTER TABLE routes ADD COLUMN elevation_pending INTEGER NOT NULL DEFAULT 0")
    old.execute("INSERT INTO gpx_files (file_id, sha256, filename, data, uploaded_at) VALUES (1, 'x', 'a.gpx', x'00', '2026-01-01')")
    old.execute("INSERT INTO routes (route_id, file_id, track_index, kind, name, created_at) VALUES (7, 1, 0, 'track', 'Ridge', '2026-01-01')")
    old.execute("PRAGMA user_version = 1")
    old.commit()

    db.init_db(old)

    assert _layout(old) == _layout(conn)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = old.execute("SELECT name, location, location_source FROM routes WHERE route_id = 7").fetchone()
    assert (row["name"], row["location"], row["location_source"]) == ("Ridge", None, None)


def test_a_version_3_database_gains_the_section_and_edge_columns_and_keeps_its_rows(conn: sqlite3.Connection, tmp_path: Path):
    old = _release_database(tmp_path / "v3.db")
    db.init_db(old)
    for table, column in (
        ("net_edges", "structure"), ("net_edges", "trail_kind"), ("net_sections", "group_id"),
        ("net_sections", "level"), ("net_section_judgments", "level"),
    ):
        old.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    old.execute(
        "INSERT INTO net_edges (edge_id, area_id, way_id, highway, surface_class, from_node_id, to_node_id, length_m, "
        "has_elevation, points_blob) VALUES (3, 1, 9, 'path', 'trail', 1, 2, 10, 0, x'')"
    )
    old.execute(
        "INSERT INTO net_sections (section_id, area_id, kind, name, length_m, centroid_lat, centroid_lng, south, west, north, east, data, edges) "
        "VALUES (7, 1, 'trail_system', 'Loop', 100, 0, 0, 0, 0, 0, 0, '{}', x'')"
    )
    old.execute(
        "INSERT INTO net_section_judgments (judgment_id, judgment, section_kind, factor, source, created_at) "
        "VALUES (5, 'adjust', 'trail_system', 1.2, 'athlete', '2026-01-01')"
    )
    old.execute("PRAGMA user_version = 3")
    old.commit()

    db.init_db(old)

    assert _layout(old) == _layout(conn)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    edge = old.execute("SELECT trail_kind, structure FROM net_edges WHERE edge_id = 3").fetchone()
    assert (edge["trail_kind"], edge["structure"]) == (None, None)
    section = old.execute("SELECT level, group_id FROM net_sections WHERE section_id = 7").fetchone()
    assert (section["level"], section["group_id"]) == ("group", None)
    assert old.execute("SELECT level FROM net_section_judgments WHERE judgment_id = 5").fetchone()["level"] == "group"


class _RacingConnection:
    """Another process adds each column between the check and our ALTER."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql.startswith("ALTER TABLE"):
            self._conn.execute(sql)
        return self._conn.execute(sql, *args)


def test_a_column_added_by_a_concurrent_upgrade_is_not_an_error_but_other_failures_are(conn: sqlite3.Connection):
    added = db.add_missing_columns(_RacingConnection(conn), "weather", "extra TEXT")  # type: ignore[arg-type]

    assert added is False  # the other process added it, so the backfills that follow are its to run
    assert "extra" in {r["name"] for r in conn.execute("PRAGMA table_info(weather)")}
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.add_missing_columns(conn, "no_such_table", "x TEXT")
    assert db.add_missing_columns(conn, "weather", "extra TEXT", "another TEXT") is True


def test_connections_use_wal_and_a_backup_taken_during_a_write_is_consistent(tmp_path: Path):
    path = tmp_path / "wal.db"
    writer = db.connect(path)
    db.init_db(writer)
    assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    writer.execute("INSERT INTO meta (key, value) VALUES ('before', '1')")
    writer.commit()
    writer.execute("INSERT INTO meta (key, value) VALUES ('uncommitted', '1')")  # an open write transaction

    reader = db.connect(path)
    assert reader.execute("SELECT value FROM meta WHERE key = 'before'").fetchone()[0] == "1"  # a reader is not blocked

    copy = sqlite3.connect(tmp_path / "copy.db")
    reader.backup(copy)
    keys = {row[0] for row in copy.execute("SELECT key FROM meta")}
    assert "before" in keys and "uncommitted" not in keys
    writer.commit()


def _gpx(n_tracks: int = 1, lat0: float = 40.0) -> bytes:
    tracks = "".join(
        "<trk><trkseg>"
        + "".join(f'<trkpt lat="{lat0 + i + j * 0.0002}" lon="-105.0"><ele>100</ele></trkpt>' for j in range(10))
        + "</trkseg></trk>"
        for i in range(n_tracks)
    )
    return f'<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">{tracks}</gpx>'.encode()


_ROUTE_CHILD_TABLES = ("route_points", "route_waypoints", "route_climbs", "route_grade_bands", "route_legs")


def _orphans(conn: sqlite3.Connection) -> int:
    return sum(
        conn.execute(f"SELECT COUNT(*) FROM {t} WHERE route_id NOT IN (SELECT route_id FROM routes)").fetchone()[0]
        for t in _ROUTE_CHILD_TABLES
    )


def test_routes_deleted_by_the_first_release_stay_deleted_and_their_orphaned_rows_go(tmp_path: Path):
    from miles import routes

    old = _release_database(tmp_path / "release.db")
    old.execute("CREATE TABLE route_tombstones (file_id INTEGER NOT NULL, track_index INTEGER NOT NULL, PRIMARY KEY (file_id, track_index))")
    db.add_missing_columns(
        old, "routes", "elevation_source TEXT NOT NULL DEFAULT 'auto'", "ele_source TEXT", "elevation_pending INTEGER NOT NULL DEFAULT 0",
            "location TEXT", "location_source TEXT",
    )
    kept = routes.upload_gpx(old, _gpx(lat0=10.0), "kept.gpx")[0]
    whole_file = routes.upload_gpx(old, _gpx(lat0=20.0), "whole.gpx")[0]
    multi = routes.upload_gpx(old, _gpx(3, lat0=30.0), "multi.gpx")
    old.execute(
        "INSERT INTO route_legs (route_id, idx, kind, leg_route_id) VALUES (?, 0, 'route', ?)", [kept, whole_file]
    )
    kept_points = old.execute("SELECT COUNT(*) FROM route_points WHERE route_id = ?", [kept]).fetchone()[0]
    # The first release deleted only the routes row.
    old.execute("DELETE FROM routes WHERE route_id IN (?, ?)", [whole_file, multi[1]])
    old.execute("DROP TABLE route_tombstones")
    for column in _ADDED_ROUTE_COLUMNS:
        old.execute(f"ALTER TABLE routes DROP COLUMN {column}")
    old.commit()
    assert _orphans(old) > 0

    db.init_db(old)
    routes.derive_all_routes(old)

    assert {r["route_id"] for r in old.execute("SELECT route_id FROM routes")} == {kept, multi[0], multi[2]}
    assert _orphans(old) == 0
    assert old.execute("SELECT leg_route_id FROM route_legs WHERE route_id = ?", [kept]).fetchone()["leg_route_id"] is None
    assert old.execute("SELECT COUNT(*) FROM route_points WHERE route_id = ?", [kept]).fetchone()[0] == kept_points


def test_a_file_stored_but_not_yet_derived_in_a_current_database_is_not_tombstoned(conn: sqlite3.Connection):
    from miles import routes

    conn.execute(
        "INSERT INTO gpx_files (sha256, filename, data, uploaded_at) VALUES ('x', 'new.gpx', ?, '2026-01-01')", [_gpx()]
    )
    conn.commit()

    db.init_db(conn)
    routes.derive_all_routes(conn)

    assert conn.execute("SELECT COUNT(*) FROM route_tombstones").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 1


_SECTION_TABLES = (
    "net_sections", "net_section_judgments", "net_section_judgment_ways", "net_section_visits", "net_section_matched",
)


def test_a_version_2_database_gains_the_section_tables_and_keeps_its_graphs(conn: sqlite3.Connection, tmp_path: Path):
    old = _release_database(tmp_path / "v2.db")
    db.init_db(old)  # a current database, then taken back to version 2
    for table in _SECTION_TABLES:
        old.execute(f"DROP TABLE {table}")
    old.execute(
        "INSERT INTO net_graphs (area_id, derive_version, built_at, node_count, edge_count, excluded_way_count) "
        "VALUES (1, 8, 'then', 0, 0, 0)"
    )
    old.execute("PRAGMA user_version = 2")
    old.commit()

    db.init_db(old)

    assert _layout(old) == _layout(conn)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert old.execute("SELECT derive_version FROM net_graphs").fetchone()[0] == 8  # rebuilt later by the version gate
