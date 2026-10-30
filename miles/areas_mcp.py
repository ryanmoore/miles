"""MCP tools for area import, registered from mcp_server.py with one call."""

import json

from mcp.server.fastmcp import FastMCP

from . import areas as areas_service
from . import db
from .errors import reporting
from .offload import threaded


def register_area_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @threaded
    @reporting(areas_service.AreaImportError, ValueError)
    def import_area(
        label: str,
        place: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
        radius_km: float | None = None,
        drive_minutes: float | None = None,
        pinned: bool | None = None,
        region: str | None = None,
        refresh: bool = False,
        refresh_geocode: bool = False,
    ) -> str:
        """
        Import the map-data and elevation cache for a region. label (required)
        is the area's name and its reuse key: a short name such as "home";
        the same label reuses the area and its cached data. Map data never
        refreshes on its own. refresh=true asks Geofabrik whether a newer
        extract exists (a conditional request) and downloads only if so;
        within 24 hours of the last contact with the region it reuses the
        extract without asking, and contacting the same region within an hour
        of the last request is refused with an error. Status reports
        data_as_of and refresh_suggested (over ~180 days old).
        refresh_geocode=true re-looks-up `place`.

        Center on `place` (free text, geocoded) or lat/lng. Size with radius_km
        (at most 60) or drive_minutes (rough 1 km/minute, capped at the same
        60 km radius, and not an isochrone). Drive-time starts for find_routes
        come only from the imported area, so the radius bounds how far a
        drive budget can reach.
        pinned=true keeps the area indefinitely; unpinned areas are collected
        after 60 days unused. When refreshing, leave pinned and region unset
        to keep what the area has.

        The regional extract is chosen automatically (smallest region, or
        several at a border); pass region (Geofabrik path, comma-separated
        for several) only when the athlete names one. Extracts over 2 GB are
        refused with the smaller regions named in the error.

        Returns immediately with status and an estimate while a background
        import runs; poll get_area_status(area_id), not in a tight loop (a
        first import can take minutes). estimate.estimated_seconds covers only
        cutting the area from an already-downloaded extract; it is null while
        the region's extract still has to download (always for a region's
        first import), and the elevation fetch and network build come after it.
        Imports queue server-side; do not start more than needed or re-issue
        one that is queued. Geocoding is rate limited: reuse
        labels, pass lat/lng once known, and do not retry a failed place in
        a loop.
        """
        with db.connection() as conn:
            return json.dumps(
                areas_service.import_area(
                    conn,
                    place=place,
                    lat=lat,
                    lng=lng,
                    radius_km=radius_km,
                    drive_minutes=drive_minutes,
                    label=label,
                    pinned=pinned,
                    region=region,
                    refresh=refresh,
                    refresh_geocode=refresh_geocode,
                )
            )

    @mcp.tool()
    def list_areas() -> str:
        """List every imported area with label, center, radius, pinned, status,
        and last-used/imported times."""
        with db.connection() as conn:
            return json.dumps(areas_service.list_areas(conn))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def get_area_status(area_id: int) -> str:
        """Import status of one area: "new", "importing", "ready", or "error"
        (with message), plus data_as_of and refresh_suggested (re-run
        import_area with refresh=true).

        While "importing", phase with done/total gives progress. Phases, in
        order: "waiting-for-download" (another import is downloading the same
        extract), "downloading" (done is bytes received; total may be null),
        "queued" (waiting for the one import slot, held by another import's
        tile, elevation, or network work), "tiles" (cutting the area from the
        extract), "elevation" (fetching elevation tiles), "network" (building
        the route network). Some phases are skipped when the data is cached."""
        with db.connection() as conn:
            return json.dumps(areas_service.get_area_status(conn, area_id))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def pin_area(area_id: int) -> str:
        """Keep an area indefinitely instead of collecting it after 60 days unused."""
        with db.connection() as conn:
            return json.dumps(areas_service.set_pinned(conn, area_id, True))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def unpin_area(area_id: int) -> str:
        """Return an area to the 60-days-unused collection lifecycle."""
        with db.connection() as conn:
            return json.dumps(areas_service.set_pinned(conn, area_id, False))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError, areas_service.AreaImportError)
    def delete_area(area_id: int) -> str:
        """Delete an area and its cached data, plus any downloaded regional
        extract no remaining area needs. Refused while the area is importing."""
        with db.connection() as conn:
            return json.dumps({"freed_extracts": areas_service.remove_area(conn, area_id)})
