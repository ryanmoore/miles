"""Training page's intensity-distribution and load/injury-risk endpoints; see
intensity.py and load.py for the math."""

from datetime import date

from fastapi import APIRouter

from . import db
from .derive import connect_derived
from .intensity import IntensityResponse, intensity_distribution
from .load import LoadResponse, load_series

router = APIRouter()

_MIN_WEEKS = 4
_MAX_WEEKS = 1040


def _clamp(weeks: int) -> int:
    return max(_MIN_WEEKS, min(weeks, _MAX_WEEKS))


@router.get("/api/intensity-distribution")
def get_intensity_distribution(weeks: int = 26, end: date | None = None) -> IntensityResponse:
    """Weekly easy/moderate/hard seconds by HR (% of max) and by heat-adjusted
    pace vs fitness zones, for the `weeks` Mondays ending with the week of
    `end` (default today)."""
    with db.connection(connect_derived) as conn:
        return intensity_distribution(conn, _clamp(weeks), end)


@router.get("/api/load")
def get_load(weeks: int = 26, end: date | None = None) -> LoadResponse:
    """Daily TRIMP load with acute:chronic ratio and monotony/strain for the
    `weeks` x 7 days ending `end` (default today)."""
    with db.connection(connect_derived) as conn:
        return load_series(conn, _clamp(weeks), end)
