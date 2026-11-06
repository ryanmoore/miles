"""Run and week notes and plan documents, the athlete's annotations: ground
truth like the plan tables. `derive_all` never touches them and no sync path
deletes them (see adr/0005-run-and-week-notes.md)."""

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from . import db, elements
from .periods import monday_of
from .races import run_sport_filter

ELEMENT_TYPES = ("strides", "hill_sprints", "reps", "progression", "descent_practice")
ELEMENT_SOURCES = ("laps", "stream", "athlete")
PLAN_STATUSES = ("as_planned", "modified", "cut_short", "skipped_part")
FUELING_KEYS = ("carbs_g", "fluid_ml", "notes")
SHORT_MAX_CHARS = 160
DESCRIPTION_CHARS = 300
RUN_NOTE_FIELDS = (
    "short", "long", "kind", "elements", "feel", "tags", "fueling", "plan_delta", "written_by", "athlete_confirmed"
)
_JSON_FIELDS = ("elements", "tags", "fueling", "plan_delta")


class NoteError(ValueError):
    """A note field failed validation; the message names the field."""


def init_notes_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_notes (
            activity_id        INTEGER PRIMARY KEY REFERENCES activities(activity_id) ON DELETE CASCADE,
            short              TEXT,
            long               TEXT,
            kind               TEXT,
            elements           TEXT,
            feel               TEXT,
            tags               TEXT,
            fueling            TEXT,
            plan_delta         TEXT,
            written_by         TEXT,
            athlete_confirmed  INTEGER NOT NULL DEFAULT 0,
            updated_at         TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS week_notes (
            week_start  TEXT PRIMARY KEY,
            tags        TEXT,
            note        TEXT,
            written_by  TEXT,
            updated_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plan_docs (
            doc_id      INTEGER PRIMARY KEY,
            plan_id     INTEGER NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            body        TEXT NOT NULL,
            start_date  TEXT,
            end_date    TEXT,
            updated_at  TEXT NOT NULL
        )
    """)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def normalize_tags(tags: object) -> list[str]:
    if not isinstance(tags, list):
        raise NoteError("tags must be a list of strings")
    out: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            raise NoteError("tags must be a list of strings")
        tag = _slug(t)
        if tag and tag not in out:
            out.append(tag)
    return out


def _text(field: str, value: object, max_chars: int | None = None) -> str:
    if not isinstance(value, str):
        raise NoteError(f"{field} must be a string")
    if max_chars is not None and len(value) > max_chars:
        raise NoteError(f"{field} must be at most {max_chars} characters (aim for about 120)")
    return value


def _one_of(field: str, value: object, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise NoteError(f"{field} must be one of {', '.join(allowed)}")
    return str(value)


def _elements(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise NoteError("elements must be a list of {type, count, duration_s, source}")
    out: list[dict[str, Any]] = []
    for e in value:
        if not isinstance(e, dict):
            raise NoteError("elements must be a list of {type, count, duration_s, source}")
        extra = set(e) - {"type", "count", "duration_s", "source"}
        if extra:
            raise NoteError(f"elements: unknown keys {sorted(extra)}")
        count = e.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise NoteError("elements: count must be a positive integer")
        duration = e.get("duration_s")
        if duration is not None and (not isinstance(duration, (int, float)) or duration <= 0):
            raise NoteError("elements: duration_s must be a positive number")
        out.append({
            "type": _one_of("elements.type", e.get("type"), ELEMENT_TYPES),
            "count": count,
            "duration_s": duration,
            "source": _one_of("elements.source", e.get("source"), ELEMENT_SOURCES),
        })
    return out


def _fueling(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(FUELING_KEYS):
        raise NoteError(f"fueling must be an object with keys from {', '.join(FUELING_KEYS)}")
    return dict(value)


def _plan_delta(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"status", "reason"}:
        raise NoteError("plan_delta must be {status, reason}")
    return {"status": _one_of("plan_delta.status", value.get("status"), PLAN_STATUSES), "reason": value.get("reason")}


def _validated(field: str, value: object) -> object:
    """The stored form of one run-note field; None clears a nullable one."""
    if value is None:
        return None
    if field in ("short", "long", "feel", "kind", "written_by"):
        text = _text(field, value, SHORT_MAX_CHARS if field == "short" else None)
        return _slug(text) if field == "kind" else text
    if field == "athlete_confirmed":
        if not isinstance(value, bool):
            raise NoteError("athlete_confirmed must be true or false")
        return int(value)
    stored = {"elements": _elements, "tags": normalize_tags, "fueling": _fueling, "plan_delta": _plan_delta}[field](value)
    return json.dumps(stored)


def short_description(text: str | None) -> str | None:
    """The athlete's own Strava description, whitespace-collapsed and cut to DESCRIPTION_CHARS."""
    flat = " ".join((text or "").split())
    return (flat if len(flat) <= DESCRIPTION_CHARS else flat[: DESCRIPTION_CHARS - 1].rstrip() + "…") or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _upsert(conn: sqlite3.Connection, table: str, key_col: str, key: object, values: dict[str, object]) -> None:
    """Insert the row, or overwrite just the given columns of the existing one."""
    columns = {**values, "updated_at": _now()}
    conn.execute(
        f"INSERT INTO {table} ({key_col}, {', '.join(columns)}) VALUES (?{', ?' * len(columns)}) "
        f"ON CONFLICT({key_col}) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in columns)}",
        [key, *columns.values()],
    )
    conn.commit()


def set_run_note(conn: sqlite3.Connection, activity_id: int, fields: dict[str, object]) -> dict[str, object]:
    """Overwrite the provided fields of an activity's note in place (an explicit
    None clears a nullable field). The first write for an activity needs `short`."""
    unknown = set(fields) - set(RUN_NOTE_FIELDS)
    if unknown:
        raise NoteError(f"unknown fields {sorted(unknown)}; allowed: {', '.join(RUN_NOTE_FIELDS)}")
    sport_clause, params = run_sport_filter()
    if conn.execute(f"SELECT 1 FROM activities WHERE activity_id = ? AND {sport_clause}", [activity_id, *params]).fetchone() is None:
        raise NoteError(f"activity {activity_id} is not a synced run")
    values = {k: _validated(k, v) for k, v in fields.items()}
    exists = conn.execute("SELECT 1 FROM run_notes WHERE activity_id = ?", [activity_id]).fetchone() is not None
    if not values.get("short") and ("short" in values or not exists):
        raise NoteError("short is required (every run gets one)")
    if values.get("athlete_confirmed") is None:
        values.pop("athlete_confirmed", None)
    _upsert(conn, "run_notes", "activity_id", activity_id, values)
    return get_run_notes(conn, activity_ids=[activity_id])[0]


def _decoded(row: sqlite3.Row) -> dict[str, object]:
    out = {k: row[k] for k in row.keys() if row[k] is not None}
    for k in _JSON_FIELDS:
        if k in out:
            out[k] = json.loads(str(out[k]))
    if "athlete_confirmed" in out:
        out["athlete_confirmed"] = bool(out["athlete_confirmed"])
    return out


def get_run_notes(
    conn: sqlite3.Connection,
    start_date: str | None = None,
    end_date: str | None = None,
    activity_ids: list[int] | None = None,
) -> list[dict[str, object]]:
    """Notes joined to their run's date and name, oldest first; nulls omitted."""
    conditions, params = ["1"], []
    if start_date:
        conditions.append("a.start_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("DATE(a.start_date) <= ?")
        params.append(end_date)
    if activity_ids is not None:
        conditions.append(f"n.activity_id IN ({','.join('?' * len(activity_ids))})")
        params.extend(activity_ids)
    rows = conn.execute(
        f"""
        SELECT n.*, DATE(a.start_date) AS date, a.name FROM run_notes n
        JOIN activities a ON a.activity_id = n.activity_id
        WHERE {' AND '.join(conditions)} ORDER BY a.start_date
        """,
        params,
    ).fetchall()
    return [_decoded(r) for r in rows]


def set_week_note(
    conn: sqlite3.Connection, week_start: str, tags: list[str] | None = None, note: str | None = None, written_by: str | None = None
) -> dict[str, object]:
    """Overwrite the provided fields of the note for the week containing
    week_start (any date; stored under its Monday). Empty tags or note clear."""
    try:
        monday = monday_of(date.fromisoformat(week_start)).isoformat()
    except ValueError as e:
        raise NoteError(f"week_start {week_start!r} is not a valid ISO date") from e
    values: dict[str, object] = {}
    if tags is not None:
        values["tags"] = json.dumps(normalize_tags(tags)) if tags else None
    if note is not None:
        values["note"] = _text("note", note) or None
    if written_by is not None:
        values["written_by"] = _text("written_by", written_by)
    _upsert(conn, "week_notes", "week_start", monday, values)
    return get_week_notes(conn, monday, monday)[0]


def get_week_notes(
    conn: sqlite3.Connection, start_date: str | None = None, end_date: str | None = None
) -> list[dict[str, object]]:
    """Week notes for weeks containing a date in [start_date, end_date], by week."""
    conditions, params = ["1"], []
    if start_date:
        conditions.append("week_start >= ?")
        params.append(monday_of(date.fromisoformat(start_date)).isoformat())
    if end_date:
        conditions.append("week_start <= ?")
        params.append(end_date)
    rows = conn.execute(
        f"SELECT * FROM week_notes WHERE {' AND '.join(conditions)} ORDER BY week_start", params
    ).fetchall()
    return [_decoded(r) for r in rows]


def list_unnoted_runs(conn: sqlite3.Connection, start_date: str, end_date: str) -> list[dict[str, object]]:
    """Runs in the range with no note, plus workouts, long runs and races whose
    note has no `long`, each with basic stats and its stream-detected elements."""
    sport_clause, params = run_sport_filter("a")
    effective = db.effective_run_type_sql("a")
    rows = conn.execute(
        f"""
        SELECT a.activity_id, a.name, DATE(a.start_date) AS date, {effective} AS type, a.workout_label,
               a.distance_m, a.moving_time_s, a.average_heartrate, a.total_elevation_gain_m, a.description, n.short
        FROM activities a LEFT JOIN run_notes n ON n.activity_id = a.activity_id
        WHERE {sport_clause} AND DATE(a.start_date) BETWEEN ? AND ?
          AND (n.activity_id IS NULL OR (n.long IS NULL AND {effective} IN ('workout', 'long_run', 'race')))
        ORDER BY a.start_date
        """,
        [*params, start_date, end_date],
    ).fetchall()
    detected = elements.detect_for_activities(conn, [r["activity_id"] for r in rows])
    runs = (
        {
            **{k: r[k] for k in ("activity_id", "name", "date", "type", "workout_label", "moving_time_s", "short")},
            "miles": round(r["distance_m"] / elements.M_PER_MILE, 2) if r["distance_m"] else None,
            "avg_hr": round(r["average_heartrate"]) if r["average_heartrate"] else None,
            "vert_ft": round(r["total_elevation_gain_m"] * 3.28084) if r["total_elevation_gain_m"] else None,
            "desc": short_description(r["description"]),
            "detected_elements": detected.get(r["activity_id"]) or None,
        }
        for r in rows
    )
    return [{k: v for k, v in run.items() if v is not None} for run in runs]


PLAN_DOC_META = "doc_id, plan_id, title, start_date, end_date, updated_at"


def _iso_date(field: str, value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise NoteError(f"{field} {value!r} is not a valid ISO date") from e


def set_plan_doc(
    conn: sqlite3.Connection,
    plan_id: int | None = None,
    doc_id: int | None = None,
    title: str | None = None,
    body: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, object]:
    """Create a document on plan_id (no doc_id) or update one in place. An
    update changes only the fields given; an empty start_date clears the date
    range. A start without an end covers that single day."""
    if doc_id is None:
        if plan_id is None or conn.execute("SELECT 1 FROM plans WHERE plan_id = ?", [plan_id]).fetchone() is None:
            raise NoteError(f"plan {plan_id} does not exist")
        if not title or body is None:
            raise NoteError("title and body are required for a new document")
        current: dict[str, object] = {"title": "", "body": "", "start_date": None, "end_date": None}
    else:
        found = conn.execute("SELECT * FROM plan_docs WHERE doc_id = ?", [doc_id]).fetchone()
        if found is None:
            raise NoteError(f"document {doc_id} does not exist")
        current = dict(found)
    merged = {**current, **{k: v for k, v in (("title", title), ("body", body)) if v is not None}}
    if start_date == "":
        merged["start_date"] = merged["end_date"] = None
    else:
        if start_date is not None:
            merged["start_date"] = _iso_date("start_date", start_date)
        if end_date is not None:
            merged["end_date"] = _iso_date("end_date", end_date)
    if merged["end_date"] and not merged["start_date"]:
        raise NoteError("end_date needs a start_date")
    if merged["end_date"] and str(merged["end_date"]) < str(merged["start_date"]):
        raise NoteError("end_date must not be before start_date")
    row = [merged["title"], merged["body"], merged["start_date"], merged["end_date"], _now()]
    if doc_id is None:
        doc_id = conn.execute(
            "INSERT INTO plan_docs (title, body, start_date, end_date, updated_at, plan_id) VALUES (?, ?, ?, ?, ?, ?)",
            [*row, plan_id],
        ).lastrowid
    else:
        conn.execute("UPDATE plan_docs SET title = ?, body = ?, start_date = ?, end_date = ?, updated_at = ? WHERE doc_id = ?", [*row, doc_id])
    conn.commit()
    return dict(conn.execute(f"SELECT {PLAN_DOC_META} FROM plan_docs WHERE doc_id = ?", [doc_id]).fetchone())


def get_plan_doc(conn: sqlite3.Connection, doc_id: int) -> dict[str, object] | None:
    """One document with its body and its plan's title, or None."""
    row = conn.execute(
        "SELECT d.*, p.title AS plan_title FROM plan_docs d JOIN plans p ON p.plan_id = d.plan_id WHERE d.doc_id = ?", [doc_id]
    ).fetchone()
    return dict(row) if row else None


def list_plan_docs(conn: sqlite3.Connection, plan_id: int, on_date: str | None = None) -> list[dict[str, object]]:
    """A plan's documents without bodies, dated ones first by start. With
    on_date, only those whose range covers it."""
    where = "plan_id = ?"
    params: list[object] = [plan_id]
    if on_date is not None:
        where += " AND ? BETWEEN start_date AND COALESCE(end_date, start_date)"
        params.append(_iso_date("date", on_date))
    rows = conn.execute(
        f"SELECT {PLAN_DOC_META} FROM plan_docs WHERE {where} ORDER BY start_date IS NULL, start_date, doc_id", params
    ).fetchall()
    return [dict(r) for r in rows]


def delete_plan_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    if conn.execute("DELETE FROM plan_docs WHERE doc_id = ?", [doc_id]).rowcount == 0:
        raise NoteError(f"document {doc_id} does not exist")
    conn.commit()
