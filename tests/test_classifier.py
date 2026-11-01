import pytest

from miles.classifier import classify_laps
from miles.fitness import intensity_for_pace, zones_from_predicted

MILE = 1609.0


def _mps(pace_min_mi: float) -> float:
    return MILE / (pace_min_mi * 60)


def _laps(rows: list[tuple[float, float, float]]):
    """(distance_m, pace_min_mi, hr) rows -> classify_laps result."""
    return classify_laps(
        speeds=[_mps(p) for _, p, _ in rows],
        distances_m=[d for d, _, _ in rows],
        heartrates=[h for _, _, h in rows],
        moving_times_s=[d / _mps(p) for d, p, _ in rows],
    )


def _reps(n: int, dist: float, pace: float, hr: float, rest: tuple[float, float, float]):
    out: list[tuple[float, float, float]] = []
    for _ in range(n):
        out += [(dist, pace, hr), rest]
    return out[:-1]


# Warm-up auto-lap miles and a cool-down around reps whose rests are long enough
# (>= 200 m) to pass the lap floor: the walking-pace rest is the largest speed gap
# in the session, and used to leave the warm-up miles counted as reps.
WARMUP_REPS_COOLDOWN = (
    [(MILE, 8.7, 135), (MILE, 8.4, 144)]
    + _reps(5, 1400, 6.9, 172, (210, 20.0, 148))
    + [(210, 20.0, 148), (MILE, 9.9, 145)]
)

# Alternating halves are under the 8% gap that splits slow halves off as floats.
FLUX_COOLDOWN = (
    [(MILE, 8.1, 138), (MILE, 7.9, 145)]
    + [(805, p, h) for p, h in [(6.6, 165), (7.1, 168)] * 6]
    + [(MILE, 9.5, 143)]
)


@pytest.mark.parametrize("rows, expected", [
    (
        WARMUP_REPS_COOLDOWN,
        ["warmup"] * 2 + ["work", "recovery"] * 4 + ["work"] + ["cooldown"] * 2,
    ),
    (
        FLUX_COOLDOWN,
        ["warmup"] * 2 + ["work"] * 12 + ["cooldown"],
    ),
    (
        # No recovery outlier: warm-up and cool-down split off as before.
        [(MILE, 8.7, 135), (MILE, 8.5, 140)] + [(MILE, 6.5, 170)] * 4 + [(MILE, 9.5, 145)],
        ["warmup"] * 2 + ["work"] * 4 + ["cooldown"],
    ),
    (
        # Two quick miles in a long run with walking-pace laps: the retried split
        # would call every other mile a cool-down, so the first split stands.
        [(MILE, 6.6, 170), (MILE, 7.0, 175)] + [(MILE, 8.3, 150)] * 6 + [(MILE, 13.8, 140)] + [(MILE, 8.3, 150)] * 8,
        ["work"] * 8 + ["float"] + ["work"] * 8,
    ),
])
def test_classify_laps(rows, expected):
    assert _laps(rows) == expected


ZONES = zones_from_predicted(6.84, 6.02)  # MP 6.84, threshold 6.43, interval 6.02, rep 5.62


@pytest.mark.parametrize("pace, label", [
    (7.5, "aerobic"),
    (6.9, "MP"),
    (6.60, "threshold"),
    (6.28, "threshold"),
    (6.21, "interval"),
    (5.85, "interval"),
    (5.7, "repetition"),
    (5.3, "sprint"),
])
def test_intensity_for_pace_is_nearest_zone(pace, label):
    assert intensity_for_pace(pace, ZONES) == label
