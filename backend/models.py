"""Response schema for the catchment analysis API.

Numbers live in typed, self-describing blocks; every piece of geometry lives in
one GeoJSON ``FeatureCollection`` so the result can be dropped straight into
QGIS, Leaflet or any GIS without reshaping.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Bounds(BaseModel):
    south: float
    west: float
    north: float
    east: float


class Point(BaseModel):
    latitude: float
    longitude: float


class SourceInfo(BaseModel):
    """Where the terrain came from.

    Two kinds of source produce the same analysis: a contour survey the caller
    uploaded, or the OpenZenith elevation service queried for an area. The
    fields each one fills are marked below; ``kind`` says which to read.
    """

    kind: Literal["contour_file", "elevation_service"] = "contour_file"
    name: str = Field(description="Uploaded filename, or the area that was requested.")
    format: str = Field(description="KML or KMZ for an upload; the dataset name for the service.")
    elevation_min_m: float
    elevation_max_m: float
    bounds: Bounds

    # Contour uploads.
    contour_lines: int | None = None
    vertices: int | None = None
    elevation_levels: int | None = None
    contour_interval_m: float | None = None

    # Elevation service.
    provider: str | None = None
    sample_spacing_m: float | None = Field(
        default=None, description="Ground distance between the elevations the service returned."
    )
    tiles_fetched: int | None = None
    tile_zoom: int | None = None


class GridInfo(BaseModel):
    """The raster the terrain was reconstructed onto."""

    rows: int
    cols: int
    cell_size_m: float = Field(description="Ground length of one grid cell.")
    cell_area_m2: float
    extrapolated_fraction: float = Field(
        description="Share of cells outside the surveyed contours, filled by nearest neighbour."
    )


class TerrainInfo(BaseModel):
    min_elevation_m: float
    max_elevation_m: float
    mean_elevation_m: float
    relief_m: float
    mean_slope_deg: float
    max_slope_deg: float
    mapped_area_km2: float
    bounds: Bounds
    grid: GridInfo


class StorageInfo(BaseModel):
    """Water the natural hollow at the site holds before overflowing."""

    depth_m: float
    spill_elevation_m: float
    surface_area_m2: float
    volume_m3: float


class SiteInfo(BaseModel):
    rank: int
    latitude: float
    longitude: float
    elevation_m: float
    slope_deg: float
    depression_depth_m: float
    upstream_cells: int
    score: float = Field(description="Terrain suitability, 0-100.")
    rating: str
    score_breakdown: dict[str, float]
    storage: StorageInfo
    reasons: list[str]


class RunoffInfo(BaseModel):
    """Rational-method yield for the catchment."""

    method: str
    runoff_coefficient: float
    yield_m3_per_mm: float = Field(description="Runoff volume per millimetre of rainfall.")
    rainfall_mm: float | None = None
    runoff_m3: float | None = None


class CatchmentInfo(BaseModel):
    """The land draining to the recommended pond site."""

    outlet: Point
    area_m2: float
    area_km2: float
    area_hectares: float
    cell_count: int
    perimeter_m: float
    min_elevation_m: float
    max_elevation_m: float
    relief_m: float
    mean_slope_deg: float
    max_slope_deg: float
    longest_flow_path_m: float
    average_gradient: float = Field(description="Relief divided by flow-path length, m/m.")
    time_of_concentration_min: float = Field(description="Kirpich estimate of catchment response time.")
    share_of_map: float = Field(description="Fraction of the mapped area that drains here.")
    runoff: RunoffInfo


class Overlays(BaseModel):
    """PNG images aligned to ``terrain.bounds``, ready to drape on a web map."""

    elevation: str
    hillshade: str
    bounds: Bounds


class AnalysisOptions(BaseModel):
    resolution: int
    max_sites: int
    runoff_coefficient: float
    rainfall_mm: float | None = None
    centre: Point | None = Field(default=None, description="Set when the area was requested by coordinates.")
    area_km: float | None = Field(default=None, description="Side of the requested square, in kilometres.")


class AreaRequest(BaseModel):
    """Analyse an area by coordinates, with terrain from the elevation service.

    Give either a centre and a size, or an explicit ``bounds`` rectangle — a map
    viewport, for instance. ``bounds`` wins if both are present.
    """

    latitude: float | None = Field(default=None, ge=-89.0, le=89.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    area_km: float | None = Field(default=None, description="Side of the square to analyse, in kilometres.")
    bounds: Bounds | None = None
    resolution: int | None = None
    max_sites: int = Field(default=5, ge=1, le=20)
    runoff_coefficient: float = Field(default=0.4, gt=0.0, le=1.0)
    rainfall_mm: float | None = Field(default=None, ge=0.0)


class Place(BaseModel):
    """One result from a place-name search."""

    name: str
    latitude: float
    longitude: float
    kind: str | None = Field(default=None, description="What OpenStreetMap calls it: city, administrative, …")


class ElevationPoint(BaseModel):
    """One elevation reading, straight from the service."""

    latitude: float
    longitude: float
    elevation_m: float
    surface_type: str
    resolution_m: float | None = None
    source: str


class CatchmentAnalysis(BaseModel):
    """Full result of analysing one contour map."""

    success: bool = True
    analysis_id: str
    generated_at: str
    options: AnalysisOptions
    source: SourceInfo
    terrain: TerrainInfo
    pond_site: SiteInfo
    catchment: CatchmentInfo
    alternative_sites: list[SiteInfo]
    overlays: Overlays
    geojson: dict[str, Any] = Field(
        description="FeatureCollection: catchment boundary, pond sites, longest flow path, drainage lines."
    )
    warnings: list[str] = []


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    detail: str | None = None
