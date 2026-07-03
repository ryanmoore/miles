"""Shared race-distance taxonomy: one place that answers "what race distance is this?"

GPS reads long on certified courses, so bucket ranges skew above nominal distance.
"""

RACE_BUCKETS: list[tuple[str, float, float]] = [
    ("5K", 4800, 5600),
    ("10K", 9700, 11200),
    ("15K", 14700, 15800),
    ("10M", 15900, 17000),
    ("half", 20700, 22300),
    ("30K", 29300, 31500),
    ("marathon", 42000, 43500),
    ("50K", 49000, 52000),
]

NOMINAL_METERS: dict[str, float] = {
    "5K": 5000.0,
    "10K": 10000.0,
    "15K": 15000.0,
    "10M": 16093.4,
    "half": 21097.5,
    "30K": 30000.0,
    "marathon": 42195.0,
    "50K": 50000.0,
}

MARATHON_MIN_M: float = 42000.0
MARATHON_MAX_M: float = 43500.0


def classify_race_distance(distance_m: float | None) -> str | None:
    """Return the category of the first RACE_BUCKETS entry containing distance_m (inclusive), else None."""
    if distance_m is None:
        return None
    for category, min_m, max_m in RACE_BUCKETS:
        if min_m <= distance_m <= max_m:
            return category
    return None
