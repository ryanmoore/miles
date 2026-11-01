"""Read-only note endpoints for the activity and activities pages; notes are written through the MCP tools."""

from fastapi import APIRouter, Query

from . import db, notes
from .derive import connect_derived

router = APIRouter()

_DATE = r"^\d{4}-\d{2}-\d{2}$"


@router.get("/api/run-note")
def get_run_note(id: int) -> dict[str, object] | None:
    """The run note for one activity, or null when it has none."""
    with db.connection(connect_derived) as conn:
        found = notes.get_run_notes(conn, activity_ids=[id])
    return found[0] if found else None


@router.get("/api/week-notes")
def get_week_notes(start_date: str = Query(pattern=_DATE), end_date: str = Query(pattern=_DATE)) -> list[dict[str, object]]:
    """Week notes for weeks containing a date in the range."""
    with db.connection(connect_derived) as conn:
        return notes.get_week_notes(conn, start_date, end_date)
