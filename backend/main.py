"""Catchment Analyzer — application entry point.

Run with:  uvicorn backend.main:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import BASE_DIR, settings

# A production build of the UI, if one has been made (see the Dockerfile).
UI_DIR = BASE_DIR / "static"

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    summary="Find where a pond should go, and how much land drains to it.",
    description=(
        "Give the service terrain and it returns the best place to put a pond and "
        "the land that drains there. It reconstructs the surface, routes water "
        "across it with a D8 flow model, ranks candidate sites, and delineates the "
        "catchment of the best one.\n\n"
        "Terrain arrives two ways. **`/analyzeContour`** takes a contour map as KML "
        "or KMZ, and nothing about the analysis is specific to any one map: extent, "
        "elevation range, contour interval and grid resolution are all derived from "
        "the file. **`/analyzeArea`** takes coordinates instead and downloads the "
        "elevations from the free [OpenZenith](https://openzenith.org) service, so "
        "anywhere on Earth can be analysed with no survey to hand."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Both the prefixed and bare paths work: /api/analyzeContour and /analyzeContour.
app.include_router(router, prefix=settings.API_PREFIX)
app.include_router(router, include_in_schema=False)

# Generated elevation and hillshade overlays.
app.mount("/storage", StaticFiles(directory=settings.STORAGE_DIR), name="storage")


@app.exception_handler(Exception)
async def unhandled_error(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Analysis failed.", "detail": str(exc)},
    )


def service_info() -> dict:
    """What this service is and where its endpoints live."""
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
        "endpoints": {
            "analyze": f"{settings.API_PREFIX}/analyzeContour",
            "alias": f"{settings.API_PREFIX}/findCatchment",
            "analyze_area": f"{settings.API_PREFIX}/analyzeArea",
            "elevation": f"{settings.API_PREFIX}/elevation",
            "health": f"{settings.API_PREFIX}/health",
        },
        "accepts": list(settings.ALLOWED_EXTENSIONS),
        "terrain_service": {
            "provider": "OpenZenith",
            "url": settings.ELEVATION_API_URL,
            "enabled": settings.ELEVATION_API_ENABLED,
            "max_area_km": settings.MAX_AREA_KM,
        },
    }


# With a built UI present, "/" serves the app; otherwise it describes the service.
# Either way the API keeps its own paths, which are registered above this mount.
if (UI_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
else:
    app.get("/", tags=["Service"], summary="Service description")(service_info)
