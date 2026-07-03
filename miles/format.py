def fmt_time(seconds: int | None) -> str:
    """H:MM:SS, or "—" for None."""
    if seconds is None:
        return "—"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"
