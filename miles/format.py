import os


def app_url(path: str) -> str:
    """Absolute address of a web app path when MILES_PUBLIC_URL is set, else the path."""
    return os.environ.get("MILES_PUBLIC_URL", "").rstrip("/") + path


def fmt_time(seconds: int | None) -> str:
    """H:MM:SS, or "—" for None."""
    if seconds is None:
        return "—"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


def fmt_pace(minutes_per_mile: float) -> str:
    """Decimal minutes per mile (e.g. 6.56) -> "6:34"."""
    mins = int(minutes_per_mile)
    secs = round((minutes_per_mile - mins) * 60)
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}"
