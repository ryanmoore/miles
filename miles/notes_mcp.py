"""MCP tools for run and week notes, plan documents, stream-detected run elements, and recent history."""

import json
from datetime import date

from mcp.server.fastmcp import FastMCP

from . import db, elements, history, notes, plan
from .derive import connect_derived
from .errors import reporting


def register_notes_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @reporting(notes.NoteError)
    def set_run_note(activity_id: int, fields: dict[str, object]) -> str:
        """
        Write or update the note for one run. Notes are athlete-authored data:
        derive and re-sync never touch them, and an overwrite replaces the
        field in place (no history). Write one when the athlete supplies
        context, or when drafting from list_unnoted_runs; never invent `feel`.

        `fields` holds only the fields to change; an absent field is kept and an
        explicit null clears a nullable one. The first note for a run needs `short`.
          short: one line, at most 160 characters (aim for ~120), every run gets one
          long: a paragraph for workouts, long runs, races, anything notable
            (say what the computed summary got right or wrong)
          kind: what the run actually was (easy, long, threshold, mp, vo2, race,
            hike...), which may differ from the Strava tag
          elements: [{type: strides|hill_sprints|reps|progression|descent_practice,
            count, duration_s, source: laps|stream|athlete}]
          feel: the athlete's own words, quoted (their run description counts)
          tags: open list, normalized to lowercase snake_case (heat, altitude,
            poor_sleep, sick, travel, new_shoes...)
          fueling: {carbs_g, fluid_ml, notes}
          plan_delta: {status: as_planned|modified|cut_short|skipped_part, reason}
          written_by: who wrote it; athlete_confirmed: true once the athlete agreed
        Returns the stored note.
        """
        with db.connection(connect_derived) as conn:
            return json.dumps(notes.set_run_note(conn, activity_id, fields))

    @mcp.tool()
    def get_run_notes(
        start_date: str | None = None, end_date: str | None = None, activity_ids: list[int] | None = None
    ) -> str:
        """
        Run notes (see set_run_note) with each run's date and name, oldest first,
        filtered by date range (YYYY-MM-DD) and/or activity ids. Null fields are omitted.
        """
        with db.connection(connect_derived) as conn:
            return json.dumps(notes.get_run_notes(conn, start_date, end_date, activity_ids))

    @mcp.tool()
    def list_unnoted_runs(start_date: str, end_date: str) -> str:
        """
        Runs between two dates (YYYY-MM-DD) that still need a note: runs with no
        note, plus workouts, long runs and races whose note has no `long`. Each
        has basic stats, `desc` (the athlete's own Strava description, cut to
        300 characters; a good source for `feel`) and `detected_elements`: strides or hill sprints found in
        its stream, as proposals to confirm against the athlete before writing.
        """
        with db.connection(connect_derived) as conn:
            return json.dumps(notes.list_unnoted_runs(conn, start_date, end_date))

    @mcp.tool()
    @reporting(notes.NoteError)
    def detect_run_elements(activity_id: int) -> str:
        """
        Strides and hill sprints detected in one run's stored stream: bursts of a
        few seconds well above the run's own effort, outside any workout work
        lap. Each is {type, count, duration_s (typical), where, source: "stream"}.
        Proposals only; nothing is written to the run's note.
        """
        with db.connection(connect_derived) as conn:
            found = elements.detect_for_activities(conn, [activity_id])
        if activity_id not in found:
            raise notes.NoteError(f"activity {activity_id} has no stored stream segments")
        return json.dumps({"activity_id": activity_id, "elements": found[activity_id]})

    @mcp.tool()
    @reporting(notes.NoteError)
    def set_week_note(
        week_start: str, tags: list[str] | None = None, note: str | None = None, written_by: str | None = None
    ) -> str:
        """
        Record what life did to a training week: tags (open list, lowercase
        snake_case; typically down, travel, sick, life, taper, race) and a short
        note. Any date in the week works (stored under its Monday). Only provided
        fields change; an empty list or empty string clears. This is separate
        from log_plan_adjustment, which records changes to the plan.
        """
        with db.connection(connect_derived) as conn:
            return json.dumps(notes.set_week_note(conn, week_start, tags, note, written_by))

    @mcp.tool()
    def get_week_notes(start_date: str | None = None, end_date: str | None = None) -> str:
        """Week notes for weeks containing a date in the range (YYYY-MM-DD), oldest first."""
        with db.connection(connect_derived) as conn:
            return json.dumps(notes.get_week_notes(conn, start_date, end_date))

    @mcp.tool()
    @reporting(notes.NoteError)
    def set_plan_doc(
        plan_id: int | None = None,
        doc_id: int | None = None,
        title: str | None = None,
        body: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """
        Create (no doc_id) or update a markdown document on a plan: race-day
        fueling, carb loading, race logistics, anything the athlete wants to
        read on a given day. The app shows it on the days it covers (a start
        without an end covers that one day; no dates attach it to the plan
        only) and renders the body, tables included. plan_id defaults to the
        current plan. An update changes only the fields given and replaces
        the body in place; start_date "" clears the dates. Returns the
        document without its body.
        """
        with db.connection(connect_derived) as conn:
            if doc_id is None and plan_id is None:
                found = plan.default_plan(conn, date.today())
                plan_id = found["plan_id"] if found else None
            return json.dumps(notes.set_plan_doc(conn, plan_id, doc_id, title, body, start_date, end_date))

    @mcp.tool()
    @reporting(notes.NoteError)
    def get_plan_doc(doc_id: int) -> str:
        """One plan document with its markdown body (see set_plan_doc)."""
        with db.connection(connect_derived) as conn:
            found = notes.get_plan_doc(conn, doc_id)
        if found is None:
            raise notes.NoteError(f"document {doc_id} does not exist")
        return json.dumps(found)

    @mcp.tool()
    def list_plan_docs(plan_id: int | None = None, on_date: str | None = None) -> str:
        """
        A plan's documents (no bodies; fetch one with get_plan_doc), dated
        ones first. plan_id defaults to the current plan; on_date
        (YYYY-MM-DD) keeps only those covering that day.
        """
        with db.connection(connect_derived) as conn:
            found = plan.get_plan(conn, plan_id) if plan_id is not None else plan.default_plan(conn, date.today())
            return json.dumps(notes.list_plan_docs(conn, found["plan_id"], on_date) if found else [])

    @mcp.tool()
    @reporting(notes.NoteError)
    def delete_plan_doc(doc_id: int) -> str:
        """Delete one plan document."""
        with db.connection(connect_derived) as conn:
            notes.delete_plan_doc(conn, doc_id)
        return json.dumps({"deleted": doc_id})

    @mcp.tool()
    @reporting(ValueError)
    def get_recent_history(weeks: int = 6, end_date: str | None = None) -> str:
        """
        Recent training in one call, detail fading with age. Start here for
        "how have the last weeks gone" or "how does this workout compare".
        `weeks` (default 6, no maximum) Monday-aligned weeks ending with the
        week of end_date (default today) get full detail (`weeks`): per week
        miles vs the plan's target band (`plan`), week note (`tags`, `wnote`),
        plan adjustments (`adj`), and per run its stats, biggest single climb
        and descent (`climb`/`descent`: ft at average % grade; one sustained
        effort, a flat lead-in or a drop ends it), `desc` (the athlete's Strava
        description, cut to 300 characters), the run note
        (`note` is the long text when written, else the short; with `feel`,
        `tags`, `el`, `fuel`, `delta`) or stream-detected `el_detected`. A
        workout has `struct` (its work laps) and `vs`, an earlier session of the
        same label with pace (s/mi, negative = faster) and HR deltas: the latest
        with a similar structure (`match: structure`, rep count within 2, rep
        length within 25%), else the latest of that label in the past year
        (`match: label`). A tagged long run, or any other run of 10+ miles or
        90+ minutes, has `splits` ("mile: pace hr +ascent/-descent" in ft).
        `plan_days` lists the planned days of this week and next. Weeks back to 26 are one line each (`earlier_weeks`: miles, long
        run, sessions, strides count, tags); older history is one line per
        training period. `last_sync_at`, `days_stale` and `last_activity` (date of the newest
        synced activity) say how fresh it is.
        Detections are proposals, not facts. `struct` comes from the lap
        classifier and can be wrong; a run note's `long` is where to correct it. Paces are min/mile. `temp_f` is the Open-Meteo modelled 2 m air temperature,
        averaged over the hours the run spans at its start location (a different
        source or a reading near the start can differ by several degrees);
        `wind_kph` is Open-Meteo's average 10 m wind over the same hours.
        """
        if weeks < 1:
            raise ValueError("weeks must be at least 1")
        end = date.fromisoformat(end_date) if end_date else date.today()
        with db.connection(connect_derived) as conn:
            return json.dumps(history.recent_history(conn, weeks, end), separators=(",", ":"))
