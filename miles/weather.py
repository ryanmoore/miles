import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing_extensions import TypedDict


class WeatherRow(TypedDict):
    activity_id: int
    fetched_at: str
    temp_c_start: float | None
    temp_c_end: float | None
    temp_c_avg: float | None
    temp_c_max: float | None
    apparent_temp_c_max: float | None
    humidity_avg: float | None
    precip_mm: float | None
    wind_kph_avg: float | None
    hourly_json: str
    raw_json: str


class WeatherSpec(TypedDict):
    activity_id: int
    start_dt: datetime
    duration_s: int


def _api_url(start_date: date, end_date: date, lat: float, lng: float, use_archive: bool) -> str:
    base = (
        "https://archive-api.open-meteo.com/v1/archive"
        if use_archive
        else "https://api.open-meteo.com/v1/forecast"
    )
    params = urllib.parse.urlencode({
        "latitude": round(lat, 4),
        "longitude": round(lng, 4),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,windspeed_10m",
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
    })
    return f"{base}?{params}"


def _iter_hour_strings(start_dt: datetime, end_dt: datetime) -> Iterator[str]:
    """Yield ISO hour strings (no timezone suffix) for all hours overlapping [start_dt, end_dt)."""
    h = start_dt.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    end = end_dt.replace(tzinfo=None)
    while h < end:
        yield h.strftime("%Y-%m-%dT%H:%M")
        h += timedelta(hours=1)


def _extract_run_hours(
    time_index: dict[str, int],
    temps: list[float | None],
    apparent: list[float | None],
    humidity_list: list[float | None],
    precip_list: list[float | None],
    wind_list: list[float | None],
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, object]]:
    run_hours: list[dict[str, object]] = []
    for hour_str in _iter_hour_strings(start_dt, end_dt):
        idx = time_index.get(hour_str)
        if idx is None:
            continue
        run_hours.append({
            "hour": hour_str,
            "temp_c": temps[idx] if idx < len(temps) else None,
            "apparent_temp_c": apparent[idx] if idx < len(apparent) else None,
            "humidity_pct": humidity_list[idx] if idx < len(humidity_list) else None,
            "precip_mm": precip_list[idx] if idx < len(precip_list) else None,
            "wind_kph": wind_list[idx] if idx < len(wind_list) else None,
        })
    return run_hours


def _build_weather_row(
    run_hours: list[dict[str, object]],
    activity_id: int,
    raw_json: str,
) -> WeatherRow | None:
    if not run_hours:
        return None

    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 1) if vals else None

    def _floats(key: str) -> list[float]:
        return [float(v) for h in run_hours if isinstance(v := h[key], (int, float))]

    run_temps = _floats("temp_c")
    run_apparent = _floats("apparent_temp_c")
    run_humid = _floats("humidity_pct")
    run_wind = _floats("wind_kph")
    run_precip = _floats("precip_mm")

    return {
        "activity_id": activity_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "temp_c_start": run_temps[0] if run_temps else None,
        "temp_c_end": run_temps[-1] if run_temps else None,
        "temp_c_avg": avg(run_temps),
        "temp_c_max": max(run_temps) if run_temps else None,
        "apparent_temp_c_max": max(run_apparent) if run_apparent else None,
        "humidity_avg": avg(run_humid),
        "precip_mm": round(sum(run_precip), 2) if run_precip else None,
        "wind_kph_avg": avg(run_wind),
        "hourly_json": json.dumps(run_hours),
        "raw_json": raw_json,
    }


def fetch_weather_bulk(
    specs: list[WeatherSpec],
    lat: float,
    lng: float,
) -> list[WeatherRow]:
    """Fetch weather for multiple activities at the same location.

    Makes at most 2 API calls (one archive + one forecast) regardless of batch size.
    The archive endpoint covers all data older than 5 days; forecast covers recent runs.
    raw_json stores compact metadata (not the full response) to avoid per-activity bloat.
    """
    if not specs:
        return []

    today = date.today()
    archive_cutoff = today - timedelta(days=5)

    archive_specs = [s for s in specs if s["start_dt"].date() <= archive_cutoff]
    forecast_specs = [s for s in specs if s["start_dt"].date() > archive_cutoff]

    results: list[WeatherRow] = []

    for subset, use_archive in [(archive_specs, True), (forecast_specs, False)]:
        if not subset:
            continue

        # Date range spanning all activities in this subset (including their end times)
        all_dates: list[date] = []
        for s in subset:
            all_dates.append(s["start_dt"].date())
            all_dates.append((s["start_dt"] + timedelta(seconds=s["duration_s"])).date())
        start_date = min(all_dates)
        end_date = max(all_dates)

        url = _api_url(start_date, end_date, lat, lng, use_archive)
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            continue

        hourly = data.get("hourly", {})
        times: list[str] = hourly.get("time", [])
        temps: list[float | None] = hourly.get("temperature_2m", [])
        apparent: list[float | None] = hourly.get("apparent_temperature", [])
        humidity_list: list[float | None] = hourly.get("relative_humidity_2m", [])
        precip_list: list[float | None] = hourly.get("precipitation", [])
        wind_list: list[float | None] = hourly.get("windspeed_10m", [])

        time_index: dict[str, int] = {t: i for i, t in enumerate(times)}
        bulk_meta = json.dumps({
            "bulk_fetch": True,
            "lat": lat,
            "lng": lng,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        })

        for spec in subset:
            start_dt = spec["start_dt"]
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            end_dt = start_dt + timedelta(seconds=spec["duration_s"])

            run_hours = _extract_run_hours(
                time_index, temps, apparent, humidity_list, precip_list, wind_list,
                start_dt, end_dt,
            )
            row = _build_weather_row(run_hours, spec["activity_id"], bulk_meta)
            if row is not None:
                results.append(row)

    return results


def fetch_weather(
    activity_id: int,
    lat: float,
    lng: float,
    start_dt: datetime,
    duration_s: int,
) -> WeatherRow | None:
    """Fetch weather for a single activity. Use fetch_weather_bulk for batches."""
    rows = fetch_weather_bulk(
        [{"activity_id": activity_id, "start_dt": start_dt, "duration_s": duration_s}],
        lat, lng,
    )
    return rows[0] if rows else None
