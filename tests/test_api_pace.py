import pytest

from miles.api import _speed_to_pace


def test_speed_to_pace_known_value():
    # 26.8224 is the complete m/s -> min/mile conversion constant
    # (1609.34 / 60); dividing by 1 m/s should give exactly that.
    assert _speed_to_pace(1.0) == pytest.approx(26.8224)


def test_speed_to_pace_realistic_running_speed():
    # ~3.0 m/s is roughly an 8:56/mi pace.
    result = _speed_to_pace(3.0)
    assert result == pytest.approx(8.9408, rel=1e-4)


def test_speed_to_pace_none_returns_none():
    assert _speed_to_pace(None) is None


def test_speed_to_pace_zero_returns_none():
    # Zero speed would be a divide-by-zero / infinite pace — must be
    # guarded rather than raising or returning inf.
    assert _speed_to_pace(0.0) is None


def test_speed_to_pace_negative_returns_none():
    assert _speed_to_pace(-1.0) is None
