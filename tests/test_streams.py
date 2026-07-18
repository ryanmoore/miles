import math

import pandas as pd
import pytest
from stravalib.model import Stream

from miles.streams import (
    _build_dataframe,
    _FLAT_COST,
    _minetti_cost,
    compute_gap_pace_s_per_m,
    median_per_window,
    min_median_max_per_window,
)


def _stream(data: list[float]) -> Stream:
    # _build_dataframe does a real isinstance(x, Stream) check, so the test
    # double must be the actual stravalib model, not a duck-typed stand-in.
    return Stream(type="time", data=data, series_type="time", original_size=len(data), resolution="high")


def test_minetti_cost_flat_ground_matches_constant():
    cost = _minetti_cost(pd.Series([0.0]))
    assert cost.iloc[0] == pytest.approx(_FLAT_COST)
    assert _FLAT_COST == pytest.approx(3.6)


def test_minetti_cost_uphill_exceeds_flat():
    # Positive grade (uphill) must cost strictly more than flat ground —
    # otherwise GAP would make hills "faster" than flat, which is physically wrong.
    cost = _minetti_cost(pd.Series([0.10]))
    assert cost.iloc[0] > _FLAT_COST


def test_minetti_cost_downhill_can_cost_less_but_not_below_zero():
    # Moderate downhill (~-10%) costs less than flat; the Minetti curve's
    # minimum sits around -10% grade before cost rises again on steep descents.
    cost = _minetti_cost(pd.Series([-0.10]))
    assert 0 < cost.iloc[0] < _FLAT_COST


def test_minetti_cost_clips_extreme_grades():
    # Grades steeper than +/-45% must clip rather than extrapolate the
    # polynomial, which is only fit to a bounded range and blows up outside it.
    clipped = _minetti_cost(pd.Series([0.45]))
    unclipped_direction = _minetti_cost(pd.Series([10.0]))
    assert clipped.iloc[0] == pytest.approx(unclipped_direction.iloc[0])


def test_compute_gap_pace_flat_ground_equals_real_pace():
    # On perfectly flat ground, GAP must reduce to real pace exactly — the
    # cost ratio for grade=0 is cost(0)/cost(0) == 1.
    df = pd.DataFrame({
        "velocity_smooth_mps": [3.0] * 10,
        "grade_smooth": [0.0] * 10,
    })
    gap = compute_gap_pace_s_per_m(df)
    real_pace = 1.0 / 3.0
    assert gap.iloc[5] == pytest.approx(real_pace, rel=1e-6)


def test_compute_gap_pace_uphill_is_faster_than_real_pace():
    # Climbing should read as a faster flat-equivalent pace than real pace.
    df = pd.DataFrame({
        "velocity_smooth_mps": [3.0] * 10,
        "grade_smooth": [10.0] * 10,  # 10% grade, in percent (matches Strava's units)
    })
    gap = compute_gap_pace_s_per_m(df)
    real_pace = 1.0 / 3.0
    assert gap.iloc[5] < real_pace


def test_compute_gap_pace_zero_velocity_does_not_divide_by_zero():
    df = pd.DataFrame({
        "velocity_smooth_mps": [0.0, 3.0, 0.0],
        "grade_smooth": [0.0, 0.0, 0.0],
    })
    gap = compute_gap_pace_s_per_m(df)
    assert pd.isna(gap.iloc[0])
    assert pd.isna(gap.iloc[2])
    assert not pd.isna(gap.iloc[1])


def test_median_per_window_basic_bucketing():
    df = pd.DataFrame({
        "time_s": [0, 1, 2, 3, 4, 5],
        "heartrate": [100, 110, 120, 140, 150, 160],
    })
    # windows: [0,2) -> {100,110}, [2,4) -> {120,140}, [4,6) -> {150,160}
    result = median_per_window(df, [0, 2, 4, 6], "heartrate")
    assert result == [pytest.approx(105), pytest.approx(130), pytest.approx(155)]


def test_median_per_window_empty_window_is_none():
    df = pd.DataFrame({"time_s": [0, 5], "heartrate": [100, 200]})
    # window [1,2) contains no samples
    result = median_per_window(df, [0, 1, 2, 6], "heartrate")
    assert result[1] is None


def test_median_per_window_missing_column_returns_empty():
    df = pd.DataFrame({"time_s": [0, 1, 2]})
    assert median_per_window(df, [0, 1, 2], "heartrate") == []


def test_median_per_window_fewer_than_two_boundaries_returns_empty():
    df = pd.DataFrame({"time_s": [0, 1], "heartrate": [100, 110]})
    assert median_per_window(df, [0], "heartrate") == []


def test_median_per_window_drops_nan_before_median():
    df = pd.DataFrame({
        "time_s": [0, 1, 2],
        "heartrate": [100, None, 120],
    })
    result = median_per_window(df, [0, 3], "heartrate")
    assert result == [pytest.approx(110)]


def test_min_median_max_per_window_basic():
    df = pd.DataFrame({
        "time_s": [0, 1, 2, 3],
        "velocity_smooth_mps": [2.0, 4.0, 3.0, 5.0],
    })
    result = min_median_max_per_window(df, [0, 4], "velocity_smooth_mps")
    assert len(result) == 1
    assert result[0]["min"] == pytest.approx(2.0)
    assert result[0]["max"] == pytest.approx(5.0)
    assert result[0]["median"] == pytest.approx(3.5)


def test_min_median_max_per_window_empty_window_all_none():
    df = pd.DataFrame({"time_s": [0, 10], "heartrate": [100, 200]})
    result = min_median_max_per_window(df, [0, 1, 2, 11], "heartrate")
    assert result[1] == {"min": None, "median": None, "max": None}


def test_min_median_max_per_window_min_value_floor_excludes_low_samples():
    # Simulates a paused/GPS-glitch near-zero speed sample within a lap —
    # without the floor, it would blow out "min speed" (max pace) to near-infinity.
    df = pd.DataFrame({
        "time_s": [0, 1, 2, 3],
        "velocity_smooth_mps": [0.01, 3.0, 3.2, 2.9],
    })
    unfiltered = min_median_max_per_window(df, [0, 4], "velocity_smooth_mps")
    floored = min_median_max_per_window(df, [0, 4], "velocity_smooth_mps", min_value=1.0)
    assert unfiltered[0]["min"] == pytest.approx(0.01)
    assert floored[0]["min"] == pytest.approx(2.9)


def test_min_median_max_per_window_all_samples_below_floor_returns_none():
    df = pd.DataFrame({"time_s": [0, 1], "velocity_smooth_mps": [0.01, 0.02]})
    result = min_median_max_per_window(df, [0, 2], "velocity_smooth_mps", min_value=1.0)
    assert result[0] == {"min": None, "median": None, "max": None}


def test_build_dataframe_joins_streams_on_equal_length():
    raw = {
        "time": _stream([0, 1, 2]),
        "heartrate": _stream([100, 110, 120]),
        "velocity_smooth": _stream([3.0, 3.1, 3.2]),
    }
    df = _build_dataframe(raw)
    assert list(df["time_s"]) == [0, 1, 2]
    assert list(df["heartrate"]) == [100, 110, 120]
    assert list(df["velocity_smooth_mps"]) == [3.0, 3.1, 3.2]


def test_build_dataframe_pads_shorter_stream_with_nan():
    # Strava can omit a sensor stream's trailing samples (e.g. HR strap
    # disconnects) — the shorter stream must align from the start, not
    # error or silently misalign the whole series.
    raw = {
        "time": _stream([0, 1, 2, 3]),
        "heartrate": _stream([100, 110]),
    }
    df = _build_dataframe(raw)
    assert len(df) == 4
    assert list(df["heartrate"][:2]) == [100, 110]
    assert math.isnan(df["heartrate"][2])
    assert math.isnan(df["heartrate"][3])


def test_build_dataframe_missing_time_stream_returns_empty():
    raw = {"heartrate": _stream([100, 110])}
    df = _build_dataframe(raw)
    assert df.empty


def test_build_dataframe_missing_optional_stream_becomes_all_na():
    raw = {"time": _stream([0, 1, 2])}
    df = _build_dataframe(raw)
    assert len(df) == 3
    assert df["heartrate"].isna().all()
