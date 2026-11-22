"""MCP tools for the terrain model (adr/0003-terrain-model-and-route-search.md)."""

import functools
import json
from collections.abc import Callable
from typing import ParamSpec

from mcp.server.fastmcp import FastMCP

from . import activity_stretch, db, terrain
from . import routes as routes_service
from .errors import reporting

P = ParamSpec("P")


def _reporting_rebuild(tool: Callable[P, str]) -> Callable[P, str]:
    """Answer with an error payload, not a protocol failure, while the terrain
    model is being rebuilt."""

    @functools.wraps(tool)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return tool(*args, **kwargs)
        except terrain.TerrainRebuildingError as e:
            return json.dumps({"error": str(e), "status": "rebuilding"})

    return wrapper


def register_terrain_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @_reporting_rebuild
    def get_terrain_profile(window_days: int = terrain.ROLLING_WINDOW_DAYS) -> str:
        """
        The athlete's terrain model: per-2%-grade-bin median speed,
        grade-adjusted efficiency, walk fraction, and sample count over a
        rolling window (window_days, default 90), plus the
        walk-onset grade, comfortable-descent grade, and a confidence level.
        Bins with too little data use a generic default (sample_count 0).
        """
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.get_terrain_profile(conn, window_days=window_days))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(ValueError)
    def get_terrain_trend(
        metric: str,
        grade_min_pct: float | None = None,
        grade_max_pct: float | None = None,
        since: str | None = None,
    ) -> str:
        """
        A weekly-snapshot series for one terrain-model metric:
        "walk_onset_grade_pct" or "descent_comfort_grade_pct", or
        "median_speed_mps" / "efficiency_median" / "walk_fraction" averaged
        over grade_min_pct..grade_max_pct (both required for those). since is
        an ISO date lower bound.
        """
        grade_range = (grade_min_pct, grade_max_pct) if grade_min_pct is not None and grade_max_pct is not None else None
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.get_terrain_trend(conn, metric, grade_range=grade_range, since=since))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(terrain.TerrainDataError)
    def analyze_activity_terrain(activity_id: int) -> str:
        """
        Each climb and descent of one activity against the current terrain
        model: actual vs. expected speed for that grade/fatigue/temperature,
        as a residual and a percentile of the athlete's own history at that
        grade. Needs the activity's streams (with an altitude stream), which
        sync fetches: new runs right away, older runs through the history
        pass. If they are missing the call errors; say the run hasn't been
        processed yet. GPS is not needed. Speeds are in m/s and distances in
        metres; convert to min/mi and miles or feet when quoting.
        """
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.analyze_activity_terrain(conn, activity_id))

    @mcp.tool()
    @reporting(terrain.TerrainDataError)
    def get_activity_stretch(activity_id: int, start_m: float | None = None, end_m: float | None = None) -> str:
        """
        Where a stretch of one recorded run was: start/end coordinates, a
        capped list of points along it, elevation start/end/gain/loss/average
        grade from the recorded altitude stream and from cached map elevation
        (each labelled with its source; null where not available), and the
        named ways under it in running order with the distance range each
        covers, highway class and surface. Ways come from an imported area
        covering the run; otherwise ways is empty and area is null. Stretch
        grades are lightly smoothed, so short steep hills keep their grade.
        start_m/end_m are metres along the run (default: the whole run);
        convert to miles and feet when quoting. Needs the run's streams with
        GPS; errors for a missing or GPS-less (treadmill) run.
        """
        with db.connection(terrain.connect) as conn:
            return json.dumps(activity_stretch.activity_stretch(conn, activity_id, start_m, end_m))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(routes_service.RouteNotFoundError)
    def score_route(route_id: int) -> str:
        """
        Runnability of a stored route's grade profile under the current
        terrain model: percent run/grind/hike, longest continuous hike,
        steep-descent distance, and a gap analysis (grade histogram weighted
        by the athlete's per-bin efficiency deficit: the grades most worth
        practicing). No elevation data returns has_elevation=false. Distances
        (longest_hike_m, steep_descent_m) are metres along the route, not
        vertical; quote them in miles.
        """
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.score_route(conn, route_id))
