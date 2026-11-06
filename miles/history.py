"""`get_recent_history`: recent training in one read-only pass, with detail fading
with age: full per-run detail for the last N weeks, one line per week back to 26
weeks, one line per training period before that. Reads stored rows only."""

import sqlite3
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from . import db, elements, notes
from .fitness import FT_PER_M
from .format import fmt_pace, fmt_time
from .periods import detect_periods, monday_of
from .plan import PlanVersionBundle, PlanWeekRow, current_version_for_week
from .races import effective_race_activities, run_sport_filter, week_aggs

LINE_WEEKS = 26
LONG_RUN_MILES = 10.0
LONG_RUN_MIN = 90
CLIMB_BASELINE_PCT = 3.0
CLIMB_MIN_GAIN_M = 30.0
MATCH_LOOKBACK_DAYS = 365
MATCH_CANDIDATES = 40
PLAN_NOTE_CHARS = 100
MAX_LISTED_REPS = 12
SESSION_TYPES = ("workout", "race")
STRIDE_TYPES = ("strides", "hill_sprints")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def _pace(distance_m: float | None, seconds: float | None) -> str | None:
    if not distance_m or not seconds:
        return None
    return fmt_pace(seconds / 60.0 / (distance_m / elements.M_PER_MILE))


def _dur(seconds: float) -> str:
    return f"{int(seconds) // 60}:{round(seconds) % 60:02d}"


# --- workout structure from classified work laps -----------------------------


def _structure(conn: sqlite3.Connection, activity_id: int) -> dict[str, Any] | None:
    laps = conn.execute(
        """
        SELECT distance_m, moving_time_s, average_heartrate, max_heartrate FROM laps
        WHERE activity_id = ? AND lap_type = 'work' AND moving_time_s > 0 AND distance_m > 0 ORDER BY lap_index
        """,
        [activity_id],
    ).fetchall()
    if not laps:
        return None
    dists, times = [r["distance_m"] for r in laps], [r["moving_time_s"] for r in laps]
    hrs = [r["average_heartrate"] for r in laps if r["average_heartrate"]]
    maxes = [r["max_heartrate"] or 0 for r in laps]
    by_dist = len(laps) > 1 and _cv(dists) < _cv(times)
    if by_dist:
        rep = statistics.median(dists)
        rep_label = f"{rep / elements.M_PER_MILE:.1f}mi" if rep >= 1600 else f"{round(rep / 50) * 50}m"
    else:
        rep_label = _dur(statistics.median(times))
    brief = f"{len(laps)}×{rep_label} @ {_pace(sum(dists), sum(times))} avg"
    parts = [brief]
    if 1 < len(laps) <= MAX_LISTED_REPS:
        parts[0] += f" ({'/'.join(str(_pace(d, t)) for d, t in zip(dists, times))})"
    if len(hrs) > 1:
        parts.append(f"HR avg {round(hrs[0])}→{round(hrs[-1])}")
    top = max(maxes)
    if top:
        at = maxes.index(top)
        parts.append(f"max {round(top)}" + (" on last rep" if at == len(laps) - 1 and len(laps) > 1 else f" on rep {at + 1}"))
    return {
        "text": ", ".join(parts),
        "brief": brief,
        "n": len(laps),
        "rep_s": statistics.median(times),
        "rep_m": statistics.median(dists),
        "pace_s": sum(times) / (sum(dists) / elements.M_PER_MILE),
        "hr": statistics.fmean(hrs) if hrs else None,
    }


def _cv(values: list[float]) -> float:
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean else 0.0


def _similar(a: float, b: float) -> bool:
    return abs(a - b) <= 0.25 * max(a, b)


def _previous_session(conn: sqlite3.Connection, run: sqlite3.Row, current: dict[str, Any]) -> dict[str, Any] | None:
    """The most recent earlier session of the same kind (the same workout label,
    else the same dominant intensity) with a similar structure: rep count within
    two, rep duration or distance within 25%. A labelled session with no such
    match in the last year falls back to the latest one with the same label."""
    if run["workout_label"]:
        match, args = "a.workout_label = ?", [run["workout_label"]]
    elif run["dominant_intensity"]:
        match, args = "a.dominant_intensity = ?", [run["dominant_intensity"]]
    else:
        return None
    sport_clause, sport_args = run_sport_filter("a")
    since = (date.fromisoformat(run["date"]) - timedelta(days=MATCH_LOOKBACK_DAYS)).isoformat()
    label_match: tuple[sqlite3.Row, dict[str, Any]] | None = None
    found: tuple[sqlite3.Row, dict[str, Any], str] | None = None
    for prior in conn.execute(
        f"""
        SELECT a.activity_id, DATE(a.start_date) AS date FROM activities a
        WHERE {sport_clause} AND {match} AND a.start_date < ? AND a.activity_id != ? AND DATE(a.start_date) >= ?
          AND EXISTS (SELECT 1 FROM laps l WHERE l.activity_id = a.activity_id AND l.lap_type = 'work')
        ORDER BY a.start_date DESC LIMIT ?
        """,
        [*sport_args, *args, run["start_date"], run["activity_id"], since, MATCH_CANDIDATES],
    ).fetchall():
        structure = _structure(conn, prior["activity_id"])
        if structure is None:
            continue
        if abs(structure["n"] - current["n"]) <= 2 and (
            _similar(structure["rep_s"], current["rep_s"]) or _similar(structure["rep_m"], current["rep_m"])
        ):
            found = (prior, structure, "structure")
            break
        label_match = label_match or (prior, structure)
    if found is None and run["workout_label"] and label_match:
        found = (*label_match, "label")
    if found is None:
        return None
    prior, structure, kind = found
    return _compact({
        "id": prior["activity_id"],
        "d": prior["date"],
        "s": structure["text"],
        "match": kind,
        "pace_delta_s": round(current["pace_s"] - structure["pace_s"]),
        "hr_delta": round(current["hr"] - structure["hr"]) if current["hr"] and structure["hr"] else None,
    })


# --- stream-derived detail ---------------------------------------------------


def _best_leg(rises: list[float]) -> tuple[int, int, float] | None:
    """The contiguous run of 20 m segments with the most elevation gained above a
    CLIMB_BASELINE_PCT grade (Kadane over `rises`, metres gained per segment).
    A flat lead-in or a real drop ends the leg, so a climb is one sustained
    effort rather than a trough-to-peak span across rolling ground."""
    baseline = CLIMB_BASELINE_PCT * elements.SEGMENT_M / 100.0
    best: tuple[int, int, float] | None = None
    start, score = 0, 0.0
    for i, rise in enumerate(rises):
        if score <= 0:
            start, score = i, 0.0
        score += rise - baseline
        if score > 0 and (best is None or score > best[2]):
            best = (start, i + 1, score)
    return best


def _climb_extremes(segments: list[elements.Segment]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind, sign in (("climb", 1.0), ("descent", -1.0)):
        rises = [sign * s.grade_pct * elements.SEGMENT_M / 100.0 for s in segments]
        leg = _best_leg(rises)
        if leg is None:
            continue
        lo, hi, _ = leg
        gain = sum(rises[lo:hi])
        if gain >= CLIMB_MIN_GAIN_M:
            out[kind] = {"ft": round(gain * FT_PER_M), "pct": round(gain / ((hi - lo) * elements.SEGMENT_M) * 100, 1)}
    return out


def _mile_splits(segments: list[elements.Segment]) -> list[str]:
    miles: dict[int, list[elements.Segment]] = defaultdict(list)
    for s in segments:
        miles[int(s.dist_m // elements.M_PER_MILE)].append(s)
    last_mile = max(miles)
    out = []
    for index in sorted(miles):
        group = miles[index]
        meters = len(group) * elements.SEGMENT_M
        span_mi = (group[-1].dist_m - group[0].dist_m + elements.SEGMENT_M) / elements.M_PER_MILE
        if span_mi < 0.25:
            continue
        seconds = sum(elements.SEGMENT_M / s.speed_mps for s in group)
        hrs = [s.heartrate for s in group if s.heartrate]
        rise = sum(max(0.0, s.grade_pct) for s in group) * elements.SEGMENT_M / 100 * FT_PER_M
        fall = sum(max(0.0, -s.grade_pct) for s in group) * elements.SEGMENT_M / 100 * FT_PER_M
        label = str(index + 1) if span_mi >= 0.9 or index != last_mile else f"{index + 1} ({span_mi:.1f}mi)"
        hr = f" hr{round(statistics.fmean(hrs))}" if hrs else ""
        out.append(f"{label}: {_pace(meters, seconds)}{hr} +{round(rise)}/-{round(fall)}")
    return out


def _lap_splits(conn: sqlite3.Connection, activity_id: int) -> list[str]:
    return [
        f"{i + 1}: {_pace(r['distance_m'], r['moving_time_s'])}" + (f" hr{round(r['average_heartrate'])}" if r["average_heartrate"] else "")
        for i, r in enumerate(
            conn.execute(
                "SELECT distance_m, moving_time_s, average_heartrate FROM laps WHERE activity_id = ? "
                "AND distance_m BETWEEN 1500 AND 1700 AND moving_time_s > 0 ORDER BY lap_index",
                [activity_id],
            )
        )
    ]


# --- per-run and per-week assembly -------------------------------------------


def _note_fields(note: dict[str, object] | None, effective_type: str | None) -> dict[str, Any]:
    if note is None:
        return {}
    return _compact({
        "note": note.get("long") or note["short"],
        "kind": note.get("kind") if note.get("kind") != effective_type else None,
        "feel": note.get("feel"),
        "tags": note.get("tags"),
        "el": note.get("elements"),
        "fuel": note.get("fueling"),
        "delta": note.get("plan_delta"),
    })


def _run_detail(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    note: dict[str, object] | None,
    segments: list[elements.Segment],
    detected: list[elements.Element],
) -> dict[str, Any]:
    temp_c = run["temp_c"]
    out = {
        "d": run["date"],
        "id": run["activity_id"],
        "n": run["name"],
        "t": run["type"],
        "mi": round(run["distance_m"] / elements.M_PER_MILE, 1),
        "min": round(run["moving_time_s"] / 60) if run["moving_time_s"] else None,
        "pace": _pace(run["distance_m"], run["moving_time_s"]),
        "hr": round(run["average_heartrate"]) if run["average_heartrate"] else None,
        "temp_f": round(temp_c * 9 / 5 + 32) if temp_c is not None else None,
        "wind_kph": round(run["wind_kph"]) if run["wind_kph"] is not None else None,
        "vert": round(run["total_elevation_gain_m"] * FT_PER_M) if run["total_elevation_gain_m"] else None,
        "desc": notes.short_description(run["description"]),
        **(_climb_extremes(segments) if segments else {}),
        **_note_fields(note, run["type"]),
    }
    out["el_detected"] = [
        {k: v for k, v in e.items() if k != "source"} for e in elements.unlisted((note or {}).get("elements"), detected)
    ] or None
    structure = _structure(conn, run["activity_id"]) if run["type"] in SESSION_TYPES else None
    if structure:
        out["struct"] = structure["text"]
        out["vs"] = _previous_session(conn, run, structure)
    if _is_long(run):
        out["splits"] = _mile_splits(segments) if segments else _lap_splits(conn, run["activity_id"])
    return _compact(out)


def _is_long(run: sqlite3.Row) -> bool:
    """A tagged long run, or any other non-session run of 10 miles or 90 minutes moving."""
    return run["type"] == "long_run" or (
        run["type"] not in SESSION_TYPES
        and (
            (run["distance_m"] or 0) >= LONG_RUN_MILES * elements.M_PER_MILE
            or (run["moving_time_s"] or 0) >= LONG_RUN_MIN * 60
        )
    )


def _session_line(conn: sqlite3.Connection, run: sqlite3.Row, note: dict[str, object] | None) -> str:
    if note:
        return str(note["short"])
    structure = _structure(conn, run["activity_id"])
    if structure:
        return structure["brief"]
    return f"{run['type']} {run['distance_m'] / elements.M_PER_MILE:.1f}mi @ {_pace(run['distance_m'], run['moving_time_s'])}"


def _strides(noted: object, detected: list[elements.Element]) -> int:
    """Stride and hill-sprint count: the note's entries, plus detections of types it doesn't list."""
    return sum(e["count"] for e in elements.merged(noted, detected) if e["type"] in STRIDE_TYPES)


def _governing_week(conn: sqlite3.Connection, monday: date) -> tuple[PlanVersionBundle, PlanWeekRow] | None:
    """The plan version and its week row that governed this Monday, newest plan first."""
    for plan in conn.execute("SELECT plan_id FROM plans ORDER BY created_at DESC").fetchall():
        bundle = current_version_for_week(conn, plan["plan_id"], monday)
        week = next((w for w in bundle["weeks"] if w["week_start"] == monday.isoformat()), None) if bundle else None
        if bundle and week:
            return bundle, week
    return None


def _plan_weeks(conn: sqlite3.Connection, mondays: list[date]) -> dict[str, str]:
    """Target-mile band per Monday from whichever plan version governed that week."""
    out: dict[str, str] = {}
    for monday in mondays:
        found = _governing_week(conn, monday)
        if found and found[1]["target_miles"] is not None:
            lo, hi = found[1]["target_miles"], found[1]["target_miles_hi"]
            out[monday.isoformat()] = f"{lo:g}-{hi:g}" if hi and hi != lo else f"{lo:g}"
    return out


def _plan_days(conn: sqlite3.Connection, this_monday: date) -> list[dict[str, Any]]:
    """Planned days of this week and the next, from the version governing each week."""
    out: list[dict[str, Any]] = []
    for monday in (this_monday, this_monday + timedelta(weeks=1)):
        found = _governing_week(conn, monday)
        week_end = (monday + timedelta(days=6)).isoformat()
        for d in found[0]["days"] if found else []:
            if monday.isoformat() <= d["date"] <= week_end:
                out.append(_compact({
                    "d": d["date"],
                    "slot": d["slot"],
                    "title": d["title"],
                    "mi": d["target_miles"],
                    "note": (d["note"] or "")[:PLAN_NOTE_CHARS] or None,
                }))
    return out


def _adjustments(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT date, action, reason FROM plan_log WHERE date BETWEEN ? AND ? ORDER BY date", [start, end]):
        out[monday_of(date.fromisoformat(r["date"])).isoformat()].append(
            f"{r['date'][5:]} {r['action']}" + (f": {r['reason']}" if r["reason"] else "")
        )
    return out


def _period_lines(conn: sqlite3.Connection, before: str) -> list[dict[str, Any]]:
    periods, _ = detect_periods(week_aggs(conn))
    races = effective_race_activities(conn)
    return [
        _compact({
            "start": p["start"],
            "end": p["end"],
            "wks": p["weeks_total"],
            "avg_mi": round(p["avg_mpw_active"]),
            "peak_mi": round(p["peak_week_miles"]),
            "races": [
                f"{r['date']} {r['name']} {fmt_time(r['moving_time_s'])}"
                for r in races
                if p["start"] <= r["date"] <= p["end"]
            ],
        })
        for p in periods
        if p["start"] < before
    ]


def recent_history(conn: sqlite3.Connection, weeks: int, end: date) -> dict[str, Any]:
    this_monday = monday_of(end)
    window_start = this_monday - timedelta(weeks=weeks - 1)
    line_start = this_monday - timedelta(weeks=LINE_WEEKS)
    first = min(window_start, line_start)

    sport_clause, sport_args = run_sport_filter("a")
    runs = conn.execute(
        f"""
        SELECT a.activity_id, a.name, a.start_date, DATE(a.start_date) AS date, {db.effective_run_type_sql('a')} AS type,
               a.workout_label, a.dominant_intensity, a.distance_m, a.moving_time_s, a.average_heartrate,
               a.total_elevation_gain_m, a.description, COALESCE(w.temp_c_avg, w.temp_c_start) AS temp_c,
               w.wind_kph_avg AS wind_kph
        FROM activities a LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE {sport_clause} AND DATE(a.start_date) BETWEEN ? AND ? ORDER BY a.start_date
        """,
        [*sport_args, first.isoformat(), end.isoformat()],
    ).fetchall()
    ids = [r["activity_id"] for r in runs]
    run_notes = {n["activity_id"]: n for n in notes.get_run_notes(conn, first.isoformat(), end.isoformat())}
    week_notes = {n["week_start"]: n for n in notes.get_week_notes(conn, first.isoformat(), end.isoformat())}
    in_window = {r["activity_id"] for r in runs if r["date"] >= window_start.isoformat()}
    segments = elements.load_segments(conn, [i for i in ids if i in in_window]) if in_window else {}
    detected = elements.detect_for_activities(conn, ids)

    by_week: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in runs:
        by_week[monday_of(date.fromisoformat(r["date"])).isoformat()].append(r)
    mondays = [first + timedelta(weeks=i) for i in range((this_monday - first).days // 7 + 1)]
    plan = _plan_weeks(conn, mondays)
    adjustments = _adjustments(conn, first.isoformat(), end.isoformat())

    full: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for monday in mondays:
        key = monday.isoformat()
        week_runs = by_week.get(key, [])
        long_runs = [r for r in week_runs if _is_long(r)]
        week_note = week_notes.get(key, {})
        head = {
            "wk": key,
            "mi": round(sum(r["distance_m"] or 0 for r in week_runs) / elements.M_PER_MILE, 1),
            "plan": plan.get(key),
            "runs": len(week_runs),
        }
        if monday >= window_start:
            full.append(_compact({
                **head,
                "tags": week_note.get("tags"),
                "wnote": week_note.get("note"),
                "adj": adjustments.get(key),
                "r": [
                    _run_detail(
                        conn, r, run_notes.get(r["activity_id"]), segments.get(r["activity_id"], []),
                        detected.get(r["activity_id"], []),
                    )
                    for r in week_runs
                ],
            }))
        else:
            lines.append(_compact({
                **head,
                "long": round(max(r["distance_m"] for r in long_runs) / elements.M_PER_MILE, 1) if long_runs else None,
                "sess": [_session_line(conn, r, run_notes.get(r["activity_id"])) for r in week_runs if r["type"] in SESSION_TYPES],
                "strides": sum(
                    _strides((run_notes.get(r["activity_id"]) or {}).get("elements"), detected.get(r["activity_id"], []))
                    for r in week_runs
                ) or None,
                "tags": week_note.get("tags"),
            }))

    last_sync = db.get_last_sync_at(conn)
    return _compact({
        "as_of": end.isoformat(),
        "last_sync_at": last_sync,
        "days_stale": db.days_stale(last_sync),
        "last_activity": conn.execute("SELECT MAX(DATE(start_date)) FROM activities").fetchone()[0],
        "plan_days": _plan_days(conn, this_monday),
        "weeks": full,
        "earlier_weeks": lines,
        "periods": _period_lines(conn, line_start.isoformat()),
    })
