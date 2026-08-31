"""The end-to-end analysis: terrain in, catchment information out.

    elevation grid -> sink fill -> flow direction -> flow accumulation
                   -> pond site ranking -> catchment at the best site

Only the first step differs between the two ways in: :func:`analyse` builds the
elevation grid by interpolating an uploaded contour survey, :func:`analyse_area`
downloads it from the OpenZenith elevation service for a bounding box. From the
grid onwards both run the identical chain through :func:`_report`, so a result
means the same thing whichever way the terrain arrived.

Each stage lives in its own module and is independently usable; this file only
sequences them and assembles the response. Adding a third terrain source (a
GeoTIFF DEM, say) means one more entry point, not a rewrite of the chain.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config import settings
from ..models import (
    AnalysisOptions, Bounds, CatchmentAnalysis, CatchmentInfo, GridInfo, Overlays,
    Point, RunoffInfo, SiteInfo, SourceInfo, StorageInfo, TerrainInfo,
)
from . import elevation_api, hydrology, kml, render, siting
from .geo import bbox_around, haversine_m
from .grid import ElevationGrid, build_grid
from .siting import PondSite

# Catchments below this share of the map are too small for the drainage-line
# view to add anything, so the stream threshold adapts to the map instead.
STREAM_THRESHOLD_FRACTION = 0.01


def _bounds(south: float, west: float, north: float, east: float) -> Bounds:
    return Bounds(south=round(south, 7), west=round(west, 7), north=round(north, 7), east=round(east, 7))


def _time_of_concentration_min(flow_path_m: float, gradient: float) -> float:
    """Kirpich (1940): how long runoff takes to reach the outlet from the far edge.

    Sets how fast a pond fills in a storm, and therefore its spillway.
    """
    if flow_path_m <= 0 or gradient <= 0:
        return 0.0
    return round(0.0195 * (flow_path_m ** 0.77) * (gradient ** -0.385), 1)


def _runoff(area_m2: float, coefficient: float, rainfall_mm: float | None) -> RunoffInfo:
    """Rational method: runoff volume = rainfall depth x area x coefficient."""
    per_mm = area_m2 * coefficient / 1000.0
    return RunoffInfo(
        method="Rational method (V = P x A x C)",
        runoff_coefficient=round(coefficient, 3),
        yield_m3_per_mm=round(per_mm, 2),
        rainfall_mm=rainfall_mm,
        runoff_m3=round(per_mm * rainfall_mm, 1) if rainfall_mm else None,
    )


def _site_info(site: PondSite) -> SiteInfo:
    return SiteInfo(
        rank=site.rank,
        latitude=site.latitude,
        longitude=site.longitude,
        elevation_m=site.elevation_m,
        slope_deg=site.slope_deg,
        depression_depth_m=site.depression_depth_m,
        upstream_cells=site.upstream_cells,
        score=site.score,
        rating=site.rating,
        score_breakdown=site.component_scores,
        storage=StorageInfo(
            depth_m=site.storage.depth_m,
            spill_elevation_m=site.storage.spill_elevation_m,
            surface_area_m2=site.storage.surface_area_m2,
            volume_m3=site.storage.volume_m3,
        ),
        reasons=site.reasons,
    )


def _feature(geometry_type: str, coordinates, properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": coordinates},
        "properties": properties,
    }


def _build_geojson(catchment: hydrology.Catchment, sites: list[PondSite],
                   drainage: list[list[list[float]]]) -> dict:
    features = [
        _feature("Polygon", [catchment.boundary], {
            "type": "catchment",
            "area_m2": round(catchment.area_m2, 1),
            "area_hectares": round(catchment.area_m2 / 10_000.0, 2),
        })
    ]
    features += [
        _feature("Point", [site.longitude, site.latitude], {
            "type": "pond_site",
            "rank": site.rank,
            "score": site.score,
            "rating": site.rating,
            "elevation_m": site.elevation_m,
            "storage_volume_m3": site.storage.volume_m3,
        })
        for site in sites
    ]
    if catchment.flow_path:
        features.append(_feature("LineString", catchment.flow_path, {
            "type": "longest_flow_path",
            "length_m": round(catchment.flow_path_length_m, 1),
        }))
    if drainage:
        features.append(_feature("MultiLineString", drainage, {"type": "drainage_lines"}))

    return {"type": "FeatureCollection", "features": features}


def _write_overlays(grid: ElevationGrid, analysis_id: str) -> Overlays:
    storage = Path(settings.STORAGE_DIR)
    elevation_name = f"{analysis_id}_elevation.png"
    hillshade_name = f"{analysis_id}_hillshade.png"

    render.elevation_png(grid.z, storage / elevation_name)
    render.hillshade_png(grid.z, grid.cell_size_m, storage / hillshade_name)
    render.prune(storage, keep=settings.MAX_STORED_OVERLAYS)

    return Overlays(
        elevation=f"/storage/{elevation_name}",
        hillshade=f"/storage/{hillshade_name}",
        bounds=_bounds(grid.south, grid.west, grid.north, grid.east),
    )


def _new_id() -> str:
    return f"ca_{uuid.uuid4().hex[:10]}"


def _clamp_resolution(resolution: int | None) -> int:
    if resolution is None:
        return settings.DEFAULT_RESOLUTION
    return int(np.clip(resolution, settings.MIN_RESOLUTION, settings.MAX_RESOLUTION))


def _report(grid: ElevationGrid, source: SourceInfo, options: AnalysisOptions,
            analysis_id: str, warnings: list[str]) -> CatchmentAnalysis:
    """Route water over a finished elevation grid and describe what it does.

    Everything before this point is only about obtaining a surface; from here the
    analysis is the same whether that surface came from contours or a download.
    """
    # 1. Route water over the surface.
    routed = hydrology.conditioned_surface(grid.z)
    direction = hydrology.flow_direction(routed, grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)
    depths = hydrology.depression_depth(grid.z)

    # 2. Rank pond sites, then delineate the catchment of the best one.
    sites = siting.rank_sites(grid, accumulation, direction, depths, max_sites=options.max_sites)
    if not sites:
        raise ValueError("No viable pond site was found in this terrain.")

    best = sites[0]
    catchment = hydrology.delineate(grid, direction, (best.row, best.col))
    drainage = hydrology.stream_network(grid, direction, accumulation, STREAM_THRESHOLD_FRACTION)

    # 3. Assemble the response.
    rows, cols = grid.shape

    return CatchmentAnalysis(
        analysis_id=analysis_id,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        options=options,
        source=source,
        terrain=TerrainInfo(
            min_elevation_m=round(float(grid.z.min()), 2),
            max_elevation_m=round(float(grid.z.max()), 2),
            mean_elevation_m=round(float(grid.z.mean()), 2),
            relief_m=round(float(grid.z.max() - grid.z.min()), 2),
            mean_slope_deg=round(float(grid.slope_deg.mean()), 2),
            max_slope_deg=round(float(grid.slope_deg.max()), 2),
            mapped_area_km2=round(rows * cols * grid.cell_area_m2 / 1_000_000.0, 4),
            bounds=_bounds(grid.south, grid.west, grid.north, grid.east),
            grid=GridInfo(
                rows=rows,
                cols=cols,
                cell_size_m=grid.cell_size_m,
                cell_area_m2=round(grid.cell_area_m2, 2),
                extrapolated_fraction=grid.extrapolated_fraction,
            ),
        ),
        pond_site=_site_info(best),
        catchment=CatchmentInfo(
            outlet=Point(latitude=round(catchment.outlet[0], 6), longitude=round(catchment.outlet[1], 6)),
            area_m2=round(catchment.area_m2, 1),
            area_km2=round(catchment.area_km2, 4),
            area_hectares=round(catchment.area_m2 / 10_000.0, 2),
            cell_count=len(catchment.cells),
            perimeter_m=round(catchment.perimeter_m, 1),
            min_elevation_m=round(catchment.min_elevation_m, 2),
            max_elevation_m=round(catchment.max_elevation_m, 2),
            relief_m=round(catchment.relief_m, 2),
            mean_slope_deg=round(catchment.mean_slope_deg, 2),
            max_slope_deg=round(catchment.max_slope_deg, 2),
            longest_flow_path_m=round(catchment.flow_path_length_m, 1),
            average_gradient=round(catchment.average_gradient, 5),
            time_of_concentration_min=_time_of_concentration_min(
                catchment.flow_path_length_m, catchment.average_gradient
            ),
            share_of_map=round(len(catchment.cells) / float(grid.z.size), 4),
            runoff=_runoff(catchment.area_m2, options.runoff_coefficient, options.rainfall_mm),
        ),
        alternative_sites=[_site_info(site) for site in sites[1:]],
        overlays=_write_overlays(grid, analysis_id),
        geojson=_build_geojson(catchment, sites, drainage),
        warnings=warnings,
    )


def analyse(
    data: bytes,
    filename: str,
    resolution: int = settings.DEFAULT_RESOLUTION,
    max_sites: int = 5,
    runoff_coefficient: float = 0.4,
    rainfall_mm: float | None = None,
) -> CatchmentAnalysis:
    """Analyse an uploaded contour map and return the pond site and its catchment.

    Raises :class:`~backend.core.kml.ContourParseError` if the file cannot be
    read as a contour map, and ``ValueError`` if the terrain cannot be modelled.
    """
    resolution = _clamp_resolution(resolution)

    contours = kml.read_contours(data, filename)
    kml.validate(contours)
    grid = build_grid(contours, resolution=resolution)

    south, west, north, east = contours.bounds
    low, high = contours.elevation_range

    return _report(
        grid,
        SourceInfo(
            kind="contour_file",
            name=filename,
            format=contours.source_format,
            contour_lines=len(contours.lines),
            vertices=contours.vertex_count,
            elevation_levels=len(contours.levels),
            contour_interval_m=contours.interval_m,
            elevation_min_m=low,
            elevation_max_m=high,
            bounds=_bounds(south, west, north, east),
        ),
        AnalysisOptions(
            resolution=resolution,
            max_sites=max_sites,
            runoff_coefficient=runoff_coefficient,
            rainfall_mm=rainfall_mm,
        ),
        _new_id(),
        list(contours.warnings),
    )


def _area_extent(latitude: float | None, longitude: float | None, area_km: float | None,
                 bounds: Bounds | None) -> tuple[Bounds, Point, float]:
    """Resolve a request for an area into a bounding box, its centre and its size.

    A caller may give a centre and a square size, or a rectangle outright — a map
    viewport, typically. Either way the result is checked against the size limits
    before any terrain is downloaded, because a request that is too large is
    cheaper to refuse than to attempt.
    """
    if bounds is not None:
        if bounds.north <= bounds.south or bounds.east <= bounds.west:
            raise ValueError("The bounding box is empty: north must exceed south, and east west.")
        centre = Point(
            latitude=(bounds.north + bounds.south) / 2.0,
            longitude=(bounds.east + bounds.west) / 2.0,
        )
        width_km = haversine_m(centre.latitude, bounds.west, centre.latitude, bounds.east) / 1000.0
        height_km = haversine_m(bounds.south, centre.longitude, bounds.north, centre.longitude) / 1000.0
        size_km = max(width_km, height_km)
        box = bounds
    else:
        if latitude is None or longitude is None:
            raise ValueError("Give either a latitude and longitude, or a bounding box.")
        # `or` would read a deliberate 0 as "not given" and quietly analyse the
        # default instead, reporting a size nobody asked for. Absent is absent.
        size_km = float(settings.DEFAULT_AREA_KM if area_km is None else area_km)
        centre = Point(latitude=latitude, longitude=longitude)
        box = _bounds(*bbox_around(latitude, longitude, size_km * 1000.0))

    if size_km < settings.MIN_AREA_KM:
        raise ValueError(
            f"That area is {size_km:.2f} km across; the smallest that can be analysed is "
            f"{settings.MIN_AREA_KM} km."
        )
    if size_km > settings.MAX_AREA_KM:
        raise ValueError(
            f"That area is {size_km:.1f} km across; the largest that can be analysed is "
            f"{settings.MAX_AREA_KM} km. Ask for a smaller area."
        )
    return box, centre, round(size_km, 3)


def _service_warnings(dem: elevation_api.DemSource, grid: ElevationGrid) -> list[str]:
    """Caveats that come with terrain nobody surveyed on the ground."""
    notes = [
        f"Terrain came from {dem.provider} ({dem.dataset}) at roughly "
        f"{dem.sample_spacing_m:.0f} m sampling, not from a ground survey. Treat the pond "
        f"site as a place to go and look, not as a design."
    ]
    if dem.sample_spacing_m > grid.cell_size_m * 1.5:
        notes.append(
            f"The analysis grid is {grid.cell_size_m:.0f} m but the elevation data is "
            f"{dem.sample_spacing_m:.0f} m, so detail below that is interpolated, not measured."
        )
    if dem.nodata_fraction > 0:
        notes.append(
            f"{dem.nodata_fraction:.1%} of the area had no elevation data and was filled "
            f"with the surrounding mean."
        )
    notes.append(
        "The source is a surface model: tree canopy and buildings sit in it as ground, "
        "which can invent or hide shallow hollows."
    )
    return notes


def analyse_area(
    latitude: float | None = None,
    longitude: float | None = None,
    area_km: float | None = None,
    bounds: Bounds | None = None,
    resolution: int | None = None,
    max_sites: int = 5,
    runoff_coefficient: float = 0.4,
    rainfall_mm: float | None = None,
) -> CatchmentAnalysis:
    """Analyse an area by coordinates, with terrain from the elevation service.

    The same analysis as :func:`analyse`, for ground nobody has surveyed: the
    elevation grid is downloaded from OpenZenith instead of being rebuilt from
    contour lines. Raises ``ValueError`` for an unusable request and
    :class:`~backend.core.elevation_api.ElevationServiceError` if the service
    cannot supply the terrain.
    """
    box, centre, size_km = _area_extent(latitude, longitude, area_km, bounds)
    resolution = _clamp_resolution(resolution)

    grid, dem = elevation_api.fetch_grid(box.south, box.west, box.north, box.east, resolution)

    return _report(
        grid,
        SourceInfo(
            kind="elevation_service",
            name=f"{_coordinate_label(centre)} · {size_km:g} km across",
            format=dem.dataset,
            provider=dem.provider,
            sample_spacing_m=dem.sample_spacing_m,
            tiles_fetched=dem.tiles,
            tile_zoom=dem.zoom,
            elevation_min_m=round(float(grid.z.min()), 2),
            elevation_max_m=round(float(grid.z.max()), 2),
            bounds=box,
        ),
        AnalysisOptions(
            resolution=resolution,
            max_sites=max_sites,
            runoff_coefficient=runoff_coefficient,
            rainfall_mm=rainfall_mm,
            centre=centre,
            area_km=size_km,
        ),
        _new_id(),
        _service_warnings(dem, grid),
    )


def _coordinate_label(point: Point) -> str:
    """A point as the hemisphere-signed text a map would print."""
    lat, lon = point.latitude, point.longitude
    return f"{abs(lat):.4f}\u00b0{'N' if lat >= 0 else 'S'} {abs(lon):.4f}\u00b0{'E' if lon >= 0 else 'W'}"
