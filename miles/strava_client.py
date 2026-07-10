import os
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from stravalib import Client
from stravalib.model import Lap, SummaryActivity
from stravalib.protocol import RequestMethod
from stravalib.util.limiter import RequestRate, get_rates_from_response_headers

from .db import WORKOUT_TYPE_MAP, ActivityRow, LapRow

load_dotenv()

# Latest daily/15-min usage observed from Strava's rate-limit response headers,
# updated by _record_rate_limit on every request. Sync is single-threaded, so
# a module global is sufficient.
_latest_rate: RequestRate | None = None


def _record_rate_limit(headers: dict[str, str], method: RequestMethod) -> None:
    global _latest_rate
    rate = get_rates_from_response_headers(headers, method)
    if rate is not None:
        _latest_rate = rate


def daily_calls_remaining() -> int | None:
    """Daily Strava API calls left, per the most recent response headers seen
    in this process. None until at least one request has been made."""
    if _latest_rate is None:
        return None
    return _latest_rate.long_limit - _latest_rate.long_usage


def _refresh_and_get_token() -> str:
    client = Client()
    result = client.refresh_access_token(
        client_id=int(os.environ["STRAVA_CLIENT_ID"]),
        client_secret=os.environ["STRAVA_CLIENT_SECRET"],
        refresh_token=os.environ["STRAVA_REFRESH_TOKEN"],
    )
    new_refresh = result["refresh_token"]
    if new_refresh != os.environ["STRAVA_REFRESH_TOKEN"]:
        _write_env_key("STRAVA_REFRESH_TOKEN", new_refresh)
        os.environ["STRAVA_REFRESH_TOKEN"] = new_refresh
    return result["access_token"]


def _write_env_key(key: str, value: str) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    updated = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in lines]
    env_path.write_text("\n".join(updated) + "\n")


def _lap_row(lap: Lap, activity_id: int) -> LapRow:
    def secs(td: int | timedelta | None) -> int | None:
        if td is None:
            return None
        return int(td.total_seconds()) if isinstance(td, timedelta) else int(td)

    def flt(q: float | None) -> float | None:
        return float(q) if q is not None else None

    assert lap.id is not None, "Lap must have an id"
    assert lap.lap_index is not None, "Lap must have a lap_index"
    return {
        "lap_id": lap.id,
        "activity_id": activity_id,
        "lap_index": lap.lap_index,
        "distance_m": flt(lap.distance),
        "moving_time_s": secs(lap.moving_time),
        "average_speed_mps": flt(lap.average_speed),
        "average_heartrate": lap.average_heartrate,
        "max_heartrate": lap.max_heartrate,
        "average_cadence": lap.average_cadence,
        "total_elevation_gain_m": flt(lap.total_elevation_gain),
        "pace_zone": lap.pace_zone,
        "raw_json": lap.model_dump_json(),
    }


def _get_client() -> Client:
    access_token = _refresh_and_get_token()
    client = Client(access_token=access_token)
    client.refresh_token = os.environ.get("STRAVA_REFRESH_TOKEN", "")
    client.token_expires = int(time.time()) + 3600
    client.protocol.rate_limiter.rules.append(_record_rate_limit)
    return client


def get_activity_laps_batch(activity_ids: list[int]) -> Iterator[tuple[int, list[LapRow]]]:
    """Yield (activity_id, laps) for each id using a single token refresh."""
    client = _get_client()
    for aid in activity_ids:
        yield aid, [_lap_row(lap, aid) for lap in client.get_activity_laps(aid)]


def get_activities(after_ts: str | None = None) -> Iterator[ActivityRow]:
    client = _get_client()
    for activity in client.get_activities(after=after_ts):
        yield row_from_activity(activity)


def row_from_activity(activity: SummaryActivity) -> ActivityRow:
    wt = int(activity.workout_type or 0)
    sport = str(activity.sport_type.root) if activity.sport_type is not None else ""

    def secs(td: int | timedelta | None) -> int | None:
        if td is None:
            return None
        return int(td.total_seconds()) if isinstance(td, timedelta) else int(td)

    def flt(q: float | None) -> float | None:
        return float(q) if q is not None else None

    latlng = activity.start_latlng
    start_lat: float | None = latlng.lat if latlng is not None else None
    start_lng: float | None = latlng.lon if latlng is not None else None

    assert activity.id is not None, "Activity must have an id"
    return {
        "activity_id": activity.id,
        "name": activity.name,
        "sport_type": sport,
        "start_date": activity.start_date.isoformat() if activity.start_date else None,
        "workout_type": wt,
        "run_type": WORKOUT_TYPE_MAP.get(wt, "easy"),
        "distance_m": flt(activity.distance),
        "moving_time_s": secs(activity.moving_time),
        "elapsed_time_s": secs(activity.elapsed_time),
        "total_elevation_gain_m": flt(activity.total_elevation_gain),
        "average_speed_mps": flt(activity.average_speed),
        "max_speed_mps": flt(activity.max_speed),
        "average_heartrate": activity.average_heartrate,
        "max_heartrate": activity.max_heartrate,
        "average_cadence": activity.average_cadence,
        "gear_id": activity.gear_id,
        "strava_url": f"https://www.strava.com/activities/{activity.id}",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "start_lat": start_lat,
        "start_lng": start_lng,
        "raw_json": activity.model_dump_json(),
    }
