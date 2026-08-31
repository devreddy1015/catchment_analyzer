"""HTTP endpoints.

Thin by design: validate the request, hand it to the analysis pipeline, translate
failures into status codes. All of the terrain work lives in ``backend.core``.

Terrain reaches the pipeline two ways, and there is one route for each:
``/analyzeContour`` for an uploaded survey, ``/analyzeArea`` for a place on the
map, whose elevations are downloaded from the OpenZenith service.
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from ..config import settings
from ..core.elevation_api import ElevationServiceError, point_elevation, search_places
from ..core.kml import ContourParseError
from ..core.pipeline import analyse, analyse_area
from ..models import AreaRequest, CatchmentAnalysis, ElevationPoint, Place

router = APIRouter()

ANALYSE_RESPONSES = {
    400: {"description": "Unsupported file type or empty upload"},
    413: {"description": "File larger than the upload limit"},
    422: {"description": "File could not be read as a contour map"},
    500: {"description": "Analysis failed unexpectedly"},
}

AREA_RESPONSES = {
    422: {"description": "The area is unusable — too large, too small, or featureless"},
    502: {"description": "The elevation service could not supply terrain"},
}


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    filename = file.filename or "upload.kml"
    if not filename.lower().endswith(settings.ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=f"Upload a {' or '.join(e.upper().lstrip('.') for e in settings.ALLOWED_EXTENSIONS)} "
                   f"file. Received '{filename}'.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(data) / 1e6:.1f} MB; the limit is {settings.MAX_UPLOAD_MB} MB.",
        )
    return data, filename


async def _analyse_upload(
    file: UploadFile,
    resolution: int,
    max_sites: int,
    runoff_coefficient: float,
    rainfall_mm: float | None,
) -> CatchmentAnalysis:
    data, filename = await _read_upload(file)
    try:
        return analyse(
            data,
            filename,
            resolution=resolution,
            max_sites=max(1, min(max_sites, 20)),
            runoff_coefficient=min(max(runoff_coefficient, 0.01), 1.0),
            rainfall_mm=rainfall_mm,
        )
    except ContourParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Terrain could not be modelled: {exc}") from exc


@router.post(
    "/analyzeContour",
    response_model=CatchmentAnalysis,
    responses=ANALYSE_RESPONSES,
    tags=["Catchment"],
    summary="Analyse a contour map and return its pond site and catchment",
)
async def analyze_contour(
    file: UploadFile = File(..., description="Contour map as KML or KMZ."),
    resolution: int = Form(
        settings.DEFAULT_RESOLUTION,
        description=f"Grid cells along the longer side of the map "
                    f"({settings.MIN_RESOLUTION}-{settings.MAX_RESOLUTION}). Higher is finer but slower.",
    ),
    max_sites: int = Form(5, description="How many candidate pond sites to return."),
    runoff_coefficient: float = Form(
        0.4, description="Rational-method runoff coefficient C: ~0.15 forest, 0.4 mixed farmland, 0.8 urban.",
    ),
    rainfall_mm: float | None = Form(
        None, description="Optional rainfall depth in mm. Supplied, the response includes a runoff volume.",
    ),
) -> CatchmentAnalysis:
    """Upload a contour map and get back the catchment information needed to plan a pond.

    The file is parsed into contour lines, interpolated into an elevation grid,
    routed with a D8 flow model, and searched for the best pond site. The
    catchment reported is the land draining to that site.
    """
    return await _analyse_upload(file, resolution, max_sites, runoff_coefficient, rainfall_mm)


@router.post(
    "/findCatchment",
    response_model=CatchmentAnalysis,
    responses=ANALYSE_RESPONSES,
    tags=["Catchment"],
    summary="Alias of /analyzeContour",
)
async def find_catchment(
    file: UploadFile = File(..., description="Contour map as KML or KMZ."),
    resolution: int = Form(settings.DEFAULT_RESOLUTION),
    max_sites: int = Form(5),
    runoff_coefficient: float = Form(0.4),
    rainfall_mm: float | None = Form(None),
) -> CatchmentAnalysis:
    """Identical to `/analyzeContour`; both names are provided for convenience."""
    return await _analyse_upload(file, resolution, max_sites, runoff_coefficient, rainfall_mm)


@router.post(
    "/analyzeArea",
    response_model=CatchmentAnalysis,
    responses=AREA_RESPONSES,
    tags=["Catchment"],
    summary="Analyse an area by coordinates, with terrain from the elevation service",
)
def analyze_area(request: AreaRequest) -> CatchmentAnalysis:
    """Find the best pond site for a place on the map, with no file to upload.

    Give a centre and a size in kilometres, or a `bounds` rectangle. The elevation
    grid is downloaded from OpenZenith — free, global, and needing no key — and
    then analysed exactly as an uploaded contour map would be.

    The terrain is remotely sensed at about 30 m, so the result points at ground
    worth visiting rather than at a pond design; the response says so in
    `warnings`.
    """
    try:
        return analyse_area(
            latitude=request.latitude,
            longitude=request.longitude,
            area_km=request.area_km,
            bounds=request.bounds,
            resolution=request.resolution,
            max_sites=request.max_sites,
            runoff_coefficient=request.runoff_coefficient,
            rainfall_mm=request.rainfall_mm,
        )
    except ElevationServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/elevation",
    response_model=ElevationPoint,
    responses={502: {"description": "The elevation service could not be reached"}},
    tags=["Terrain"],
    summary="Elevation at a single point",
)
def elevation(
    lat: float = Query(..., ge=-89.0, le=89.0, description="Latitude in degrees."),
    lon: float = Query(..., ge=-180.0, le=180.0, description="Longitude in degrees."),
) -> ElevationPoint:
    """One elevation reading from the OpenZenith service.

    A single cheap request, which is what makes it worth having: it tells a
    caller whether a location is land with usable data before committing to a
    full area analysis.
    """
    try:
        return ElevationPoint(**point_elevation(lat, lon))
    except ElevationServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/places",
    response_model=list[Place],
    responses={502: {"description": "The place search could not be reached"}},
    tags=["Terrain"],
    summary="Find a place by name",
)
def places(
    q: str = Query(..., min_length=2, max_length=120, description="A place name, e.g. \"Durg\"."),
    limit: int = Query(5, ge=1, le=10, description="How many matches to return."),
) -> list[Place]:
    """Turn a place name into coordinates, so an area can be asked for by name.

    Nobody knows the latitude of their own village. An empty list means nothing
    matched, which is not an error.
    """
    try:
        return [Place(**place) for place in search_places(q, limit)]
    except ElevationServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/health", tags=["Service"], summary="Liveness check")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}
