"""Per-second Strava stream data: fetch, parquet-cache, and derive GAP/lap medians.

Raw streams only are cached to data/streams/<activity_id>.parquet — no GAP or
medians baked in, since those computations may need tuning after seeing real
charts and a baked cache would need invalidation logic.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pandas as pd
from typing_extensions import TypedDict

from . import strava_client

STREAMS_DIR = Path(os.environ.get("MILES_STREAMS_DIR", Path(__file__).parent.parent / "data" / "streams"))

_STREAM_COLUMNS = [
    "time_s", "distance_m", "altitude_m", "velocity_smooth_mps",
    "heartrate", "cadence", "grade_smooth", "moving",
]

_STRAVA_TO_COLUMN = {
    "time": "time_s",
    "distance": "distance_m",
    "altitude": "altitude_m",
    "velocity_smooth": "velocity_smooth_mps",
    "heartrate": "heartrate",
    "cadence": "cadence",
    "grade_smooth": "grade_smooth",
    "moving": "moving",
}


def _parquet_path(activity_id: int) -> Path:
    return STREAMS_DIR / f"{activity_id}.parquet"


def has_cached_streams(activity_id: int) -> bool:
    return _parquet_path(activity_id).exists()


def _build_dataframe(raw: Mapping[str, object]) -> pd.DataFrame:
    """Join streams on time_s — streams can differ in length when Strava
    omits sensor gaps, so positional concatenation would misalign samples."""
    from stravalib.model import Stream

    series: dict[str, pd.Series] = {}
    time_data: list[int] | None = None
    for strava_key, column in _STRAVA_TO_COLUMN.items():
        stream = raw.get(strava_key)
        if not isinstance(stream, Stream) or stream.data is None:
            continue
        if strava_key == "time":
            time_data = list(stream.data)
        else:
            series[column] = pd.Series(list(stream.data))

    if time_data is None:
        return pd.DataFrame(columns=_STREAM_COLUMNS)

    df = pd.DataFrame({"time_s": time_data})
    for column, s in series.items():
        if len(s) == len(df):
            df[column] = s.values
        else:
            # Shorter stream (e.g. HR sensor gap) — align by position from
            # the start; missing tail samples become NaN rather than
            # misaligning the whole series against `time`.
            df[column] = pd.Series(s.values, index=range(len(s))).reindex(range(len(df))).values

    for column in _STREAM_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    return cast(pd.DataFrame, df[_STREAM_COLUMNS])


def fetch_and_cache_streams(activity_id: int) -> pd.DataFrame:
    path = _parquet_path(activity_id)
    if path.exists():
        return pd.read_parquet(path)

    raw = strava_client.get_activity_streams_raw(activity_id)
    df = _build_dataframe(raw)

    STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df


# Minetti et al. (2002) energy cost of running on gradients, as a polynomial
# in grade fraction (not percent). Cost is in J/(kg*m); GAP = pace divided by
# the ratio of graded cost to flat-ground cost, holding effort constant —
# uphill (higher cost) yields a faster GAP, downhill (lower cost) a slower one.
def _minetti_cost(grade_fraction: pd.Series) -> pd.Series:
    i = grade_fraction.clip(-0.45, 0.45)
    cost = (
        155.4 * i**5
        - 30.4 * i**4
        - 43.3 * i**3
        + 46.3 * i**2
        + 19.5 * i
        + 3.6
    )
    return cast(pd.Series, cost)


_FLAT_COST: float = float(cast(float, _minetti_cost(pd.Series([0.0])).iloc[0]))


def compute_gap_pace_s_per_m(df: pd.DataFrame) -> pd.Series:
    """Grade-adjusted pace (seconds per meter) from velocity_smooth + grade_smooth.

    grade_smooth is smoothed again here (short centered rolling mean) before
    the Minetti cost function — the nonlinear cost curve biases the mean
    upward under sample-to-sample GPS/altitude noise, so smoothing must
    happen on the input, not on the resulting GAP values (which would blur
    per-lap medians).
    """
    velocity = df["velocity_smooth_mps"]
    grade = df["grade_smooth"].astype(float).rolling(window=7, center=True, min_periods=1).mean()
    cost_ratio = _minetti_cost(grade / 100.0) / _FLAT_COST
    real_pace_s_per_m = 1.0 / velocity.replace(0, pd.NA)
    return real_pace_s_per_m / cost_ratio


def median_per_window(
    df: pd.DataFrame,
    boundaries_s: list[float],
    value_column: str,
) -> list[float | None]:
    """Median of value_column within each [boundaries_s[i], boundaries_s[i+1])
    window. Returns len(boundaries_s) - 1 values; empty windows -> None."""
    if value_column not in df.columns or len(boundaries_s) < 2:
        return []
    results: list[float | None] = []
    for start, end in zip(boundaries_s[:-1], boundaries_s[1:]):
        mask = (df["time_s"] >= start) & (df["time_s"] < end)
        window = cast(pd.Series, df.loc[mask, value_column]).dropna()
        results.append(float(cast(float, window.median())) if len(window) else None)
    return results


class WindowStats(TypedDict):
    min: float | None
    median: float | None
    max: float | None


def min_median_max_per_window(
    df: pd.DataFrame,
    boundaries_s: list[float],
    value_column: str,
    min_value: float | None = None,
) -> list[WindowStats]:
    """Min/median/max of value_column within each [boundaries_s[i], boundaries_s[i+1])
    window. Returns len(boundaries_s) - 1 entries; empty windows -> all None.

    min_value excludes samples below it before computing stats — for
    velocity_smooth_mps, near-zero readings from pauses/GPS glitches would
    otherwise blow out max pace (min speed) to absurd values.
    """
    if value_column not in df.columns or len(boundaries_s) < 2:
        return []
    results: list[WindowStats] = []
    for start, end in zip(boundaries_s[:-1], boundaries_s[1:]):
        mask = (df["time_s"] >= start) & (df["time_s"] < end)
        window = cast(pd.Series, df.loc[mask, value_column]).dropna()
        if min_value is not None:
            window = cast(pd.Series, window[window >= min_value])
        if len(window):
            results.append(WindowStats(
                min=float(cast(float, window.min())),
                median=float(cast(float, window.median())),
                max=float(cast(float, window.max())),
            ))
        else:
            results.append(WindowStats(min=None, median=None, max=None))
    return results
