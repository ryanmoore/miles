from miles.format import fmt_pace, fmt_time


def test_fmt_pace_basic():
    assert fmt_pace(6.56) == "6:34"


def test_fmt_pace_rounds_seconds_to_60_carries_into_minutes():
    # 10.999 minutes -> 10 min + 59.94 sec, which rounds to 60 sec and must
    # carry into the next minute rather than displaying "10:60".
    assert fmt_pace(10.999) == "11:00"


def test_fmt_pace_zero():
    assert fmt_pace(0.0) == "0:00"


def test_fmt_time_none_is_dash():
    assert fmt_time(None) == "—"


def test_fmt_time_under_an_hour():
    assert fmt_time(754) == "0:12:34"


def test_fmt_time_over_an_hour():
    assert fmt_time(9994) == "2:46:34"
