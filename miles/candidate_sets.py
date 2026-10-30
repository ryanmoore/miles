"""Shareable sets of route-search candidates: an ordered list with a title and
a note each, read by candidates.html.

A set outlives the search's candidate TTL, so sharing it extends the expiry
of every candidate in it to the set's own.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing_extensions import TypedDict

from . import route_search

SET_TTL_DAYS = 7


class CandidateSetNotFoundError(LookupError):
    pass


class CandidateStart(TypedDict):
    label: str | None
    lat: float
    lng: float
    drive_minutes: float | None


class CandidateCard(TypedDict):
    candidate_id: str
    note: str | None
    shape: str
    distance_m: float
    gain_m: float | None
    run_pct: float
    grind_pct: float
    hike_pct: float
    singletrack_m: float
    track_m: float
    paved_m: float
    sections_used: list[dict]
    start: CandidateStart
    ranking: dict | None  # the terms of the search's score; absent on walk routes
    missed_constraints: list[route_search.ConstraintMiss]  # empty unless a near miss


class CandidateSetView(TypedDict):
    set_id: str
    title: str
    created_at: str
    expires_at: str
    candidates: list[CandidateCard]
    request: dict | None  # the search that produced the set, when it came from one


def init_candidate_set_schema(conn: sqlite3.Connection) -> None:
    # data is {"candidate_ids": [...], "notes": {candidate_id: note}, "request": search request or null}.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_sets (
            set_id     TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            data       TEXT NOT NULL
        )
    """)


def create_set(
    conn: sqlite3.Connection, candidate_ids: list[str], title: str, notes: dict[str, str] | None, request: dict | None = None
) -> tuple[str, str]:
    """(set_id, expires_at). Raises route_search.CandidateNotFoundError for a
    candidate that is unknown or expired, and ValueError for an empty list."""
    if not candidate_ids:
        raise ValueError("a set needs at least one candidate")
    for candidate_id in candidate_ids:
        route_search.get_route_candidate(conn, candidate_id)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=SET_TTL_DAYS)).isoformat()
    marks = ",".join("?" for _ in candidate_ids)
    conn.execute(
        f"UPDATE net_candidates SET expires_at = ? WHERE expires_at < ? AND candidate_id IN ({marks})",
        [expires_at, expires_at, *candidate_ids],
    )
    set_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO candidate_sets (set_id, title, created_at, expires_at, data) VALUES (?, ?, ?, ?, ?)",
        [set_id, title, now.isoformat(), expires_at, json.dumps({"candidate_ids": candidate_ids, "notes": notes or {}, "request": request})],
    )
    conn.commit()
    return set_id, expires_at


def card(candidate: route_search.RouteCandidateOut, note: str | None) -> CandidateCard:
    score = candidate["score"]
    runnability = score["runnability"]
    start = candidate["start"]
    drive_s = start.get("drive_s")
    return {
        "candidate_id": candidate["candidate_id"],
        "note": note,
        "shape": candidate["shape"],
        "distance_m": candidate["distance_m"],
        "gain_m": score.get("gain_m"),
        "run_pct": runnability["run_pct"],
        "grind_pct": runnability["grind_pct"],
        "hike_pct": runnability["hike_pct"],
        "singletrack_m": score.get("singletrack_distance_m", 0.0),
        "track_m": score.get("track_distance_m", 0.0),
        "paved_m": score["paved_distance_m"],
        "sections_used": candidate.get("sections_used", []),
        "ranking": candidate.get("ranking"),
        "missed_constraints": candidate["missed_constraints"],
        "start": {
            "label": start.get("label"),
            "lat": start["lat"],
            "lng": start["lng"],
            "drive_minutes": drive_s / 60.0 if drive_s is not None else None,
        },
    }


def get_set(conn: sqlite3.Connection, set_id: str) -> CandidateSetView:
    """Raises CandidateSetNotFoundError for an unknown or expired set."""
    conn.execute("DELETE FROM candidate_sets WHERE expires_at < ?", [datetime.now(timezone.utc).isoformat()])
    conn.commit()
    row = conn.execute("SELECT * FROM candidate_sets WHERE set_id = ?", [set_id]).fetchone()
    if row is None:
        raise CandidateSetNotFoundError(f"no candidate set {set_id} (or it expired)")
    data = json.loads(row["data"])
    return {
        "set_id": set_id,
        "title": row["title"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "candidates": [
            card(route_search.get_route_candidate(conn, cid), data["notes"].get(cid)) for cid in data["candidate_ids"]
        ],
        "request": data.get("request"),
    }
