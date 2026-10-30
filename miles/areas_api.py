"""HTTP area list and the receiving end of miles-area-import. No auth, like every other endpoint."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import areas as areas_service
from . import db, geocode

router = APIRouter()


class ImportTileBody(BaseModel):
    gx: int
    gy: int
    elements: list[dict]


class ImportTilesBody(BaseModel):
    label: str
    center_lat: float
    center_lng: float
    radius_km: float
    tiles: list[ImportTileBody]
    pinned: bool | None = None
    source: str = "extract:cli"


@router.get("/api/areas")
def list_areas() -> list[areas_service.AreaSummary]:
    with db.connection() as conn:
        return areas_service.list_areas(conn)


class ImportBody(BaseModel):
    label: str
    lat: float
    lng: float
    radius_km: float


class PinBody(BaseModel):
    pinned: bool


@router.get("/api/geocode")
def get_geocode(q: str) -> geocode.GeocodeResult:
    """One explicit lookup per call; the pages never call it as the user types."""
    try:
        with db.connection() as conn:
            return geocode.lookup(conn, q)
    except geocode.NoMatchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except geocode.GeocodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/areas/estimate")
def get_estimate(lat: float, lng: float, radius_km: float) -> areas_service.ImportEstimate:
    try:
        with db.connection() as conn:
            return areas_service.estimate_for(conn, lat, lng, radius_km)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/areas")
def post_area(body: ImportBody) -> areas_service.AreaStatus:
    try:
        with db.connection() as conn:
            return areas_service.import_area(
                conn, label=body.label, lat=body.lat, lng=body.lng, radius_km=body.radius_km)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/areas/{area_id}/pin")
def post_pin(area_id: int, body: PinBody) -> areas_service.AreaDetail:
    try:
        with db.connection() as conn:
            return areas_service.set_pinned(conn, area_id, body.pinned)
    except areas_service.AreaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/api/areas/{area_id}")
def delete_area(area_id: int) -> dict[str, list[str]]:
    try:
        with db.connection() as conn:
            return {"freed_extracts": areas_service.remove_area(conn, area_id)}
    except areas_service.AreaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except areas_service.AreaImportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/areas/import-tiles")
def post_import_tiles(body: ImportTilesBody) -> areas_service.AreaStatus:
    """Receiving end of miles-area-import: tiles already cut locally; skips to
    the elevation and network-build tail."""
    try:
        with db.connection() as conn:
            return areas_service.import_tiles(
                conn,
                label=body.label,
                center_lat=body.center_lat,
                center_lng=body.center_lng,
                radius_km=body.radius_km,
                tile_payloads=[{"gx": t.gx, "gy": t.gy, "elements": t.elements} for t in body.tiles],
                pinned=body.pinned,
                source=body.source,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
