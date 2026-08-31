"""Terrain downloaded from the OpenZenith elevation service.

These tests never touch the network. A mock transport answers tile requests with
a *synthetic hill computed from each pixel's real latitude and longitude*, which
is the point: it means the assertions below check the web-mercator arithmetic
that turns a bounding box into tiles and back into a grid. If that maths were
wrong the downloaded surface would be shifted or scaled, and a hill sampled from
a known analytic function is the only way to notice.

The one test that does call the live service is skipped unless
``ELEVATION_LIVE_TESTS=1`` is set.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from backend.config import settings
from backend.core import elevation_api
from backend.core.elevation_api import ElevationServiceError, TILE_PX
from backend.core.geo import bbox_around, haversine_m

from .conftest import (
    HILL_BASE_M, HILL_HEIGHT_M, HILL_LAT, HILL_LON, FakeService, below_sea_level, flat_ground,
    hill_elevation, no_data, seabed,
)

# --------------------------------------------------------------------------- #
# Web mercator                                                                 #
# --------------------------------------------------------------------------- #

def test_pixel_of_places_the_origin_at_the_top_left() -> None:
    x, y = elevation_api.pixel_of(-180.0, elevation_api.MAX_MERCATOR_LAT, zoom=0)
    assert x == pytest.approx(0.0, abs=1e-6)
    assert y == pytest.approx(0.0, abs=0.01)


def test_pixel_of_puts_null_island_in_the_middle() -> None:
    x, y = elevation_api.pixel_of(0.0, 0.0, zoom=3)
    assert (x, y) == pytest.approx((TILE_PX * 4, TILE_PX * 4))


@pytest.mark.parametrize("zoom", range(4, 15))
def test_pixel_size_halves_with_every_zoom_level(zoom: int) -> None:
    assert elevation_api.pixel_size_m(0.0, zoom) == pytest.approx(
        elevation_api.pixel_size_m(0.0, zoom - 1) / 2.0
    )


@pytest.mark.parametrize("cell_size_m", [20.0, 90.0, 400.0, 2000.0])
def test_zoom_for_asks_for_pixels_finer_than_the_grid_cell(cell_size_m: float) -> None:
    """The chosen zoom must resolve a cell, and be no finer than it needs to be."""
    zoom = elevation_api.zoom_for(cell_size_m, lat=21.25)
    assert elevation_api.pixel_size_m(21.25, zoom) <= cell_size_m
    assert elevation_api.pixel_size_m(21.25, zoom - 1) > cell_size_m


def test_zoom_never_exceeds_the_resolution_of_the_data() -> None:
    assert elevation_api.zoom_for(0.01, lat=0.0) == settings.ELEVATION_MAX_ZOOM


def test_tile_span_covers_the_whole_box() -> None:
    south, west, north, east = bbox_around(HILL_LAT, HILL_LON, 3000.0)
    x0, y0, x1, y1 = elevation_api.tile_span(south, west, north, east, zoom=13)

    left, top = elevation_api.pixel_of(west, north, 13)
    right, bottom = elevation_api.pixel_of(east, south, 13)
    assert x0 * TILE_PX <= left and right <= (x1 + 1) * TILE_PX
    assert y0 * TILE_PX <= top and bottom <= (y1 + 1) * TILE_PX


def test_bbox_around_is_square_on_the_ground() -> None:
    south, west, north, east = bbox_around(HILL_LAT, HILL_LON, 4000.0)
    width = haversine_m(HILL_LAT, west, HILL_LAT, east)
    height = haversine_m(south, HILL_LON, north, HILL_LON)
    assert width == pytest.approx(4000.0, rel=0.01)
    assert height == pytest.approx(4000.0, rel=0.01)


# --------------------------------------------------------------------------- #
# Downloading a grid                                                           #
# --------------------------------------------------------------------------- #

def test_downloaded_grid_matches_the_surface_it_sampled(service: FakeService) -> None:
    """The grid must reproduce the analytic hill at the right places.

    This is the projection test: an error anywhere between bounding box, tile
    numbers, pixel offsets and interpolation shifts the surface, and comparing
    against the function the service was answering with is what catches it.
    """
    south, west, north, east = bbox_around(HILL_LAT, HILL_LON, 3000.0)
    grid, source = elevation_api.fetch_grid(south, west, north, east, resolution=120)

    lons, lats = np.meshgrid(grid.lons, grid.lats)
    expected = hill_elevation(lats, lons)

    assert np.abs(grid.z - expected).max() < 2.0
    assert np.abs(grid.z - expected).mean() < 0.5
    assert source.provider == "OpenZenith"
    assert source.nodata_fraction == 0.0


def test_elevations_are_read_from_the_middle_of_the_pixel(dem_service) -> None:
    """A stored pixel is a reading at the centre of the square it covers.

    On a plane, bilinear interpolation is exact, so any residual is a sampling
    offset rather than interpolation error: reading pixel corners instead of
    centres would bias the whole surface by half a pixel of slope. A slope this
    steep turns that into about half a metre, which the tolerance below excludes.
    """
    dem_service(surface=lambda lat, lon: 100.0 + 20_000.0 * (np.asarray(lon) - HILL_LON))
    grid, _ = elevation_api.fetch_grid(*bbox_around(HILL_LAT, HILL_LON, 2000.0), resolution=100)

    lons, lats = np.meshgrid(grid.lons, grid.lats)
    expected = 100.0 + 20_000.0 * (lons - HILL_LON)

    # The edges are smoothed against the grid border, so judge the interior.
    error = (grid.z - expected)[3:-3, 3:-3]
    assert abs(float(error.mean())) < 0.15
    assert abs(error).max() < 1.0


def test_the_summit_lands_where_the_summit_is(service: FakeService) -> None:
    south, west, north, east = bbox_around(HILL_LAT, HILL_LON, 3000.0)
    grid, _ = elevation_api.fetch_grid(south, west, north, east, resolution=140)

    row, col = np.unravel_index(int(np.argmax(grid.z)), grid.shape)
    lat, lon = grid.point_at(int(row), int(col))
    assert haversine_m(lat, lon, HILL_LAT, HILL_LON) < 2.5 * grid.cell_size_m


def test_grid_geometry_is_the_same_as_a_contour_grid(service: FakeService) -> None:
    """Whichever way terrain arrives, the raster under it is built the same way."""
    south, west, north, east = bbox_around(HILL_LAT, HILL_LON, 3000.0)
    grid, _ = elevation_api.fetch_grid(south, west, north, east, resolution=100)

    assert max(grid.shape) == 100
    assert grid.cell_size_m == pytest.approx(3000.0 / 100, rel=0.05)
    assert (grid.south, grid.west, grid.north, grid.east) == (south, west, north, east)


def test_tiles_are_cached_between_requests(service: FakeService) -> None:
    box = bbox_around(HILL_LAT, HILL_LON, 2000.0)
    elevation_api.fetch_grid(*box, resolution=80)
    after_first = service.tile_requests
    assert after_first > 0

    elevation_api.fetch_grid(*box, resolution=80)
    assert service.tile_requests == after_first, "cached tiles should not be downloaded twice"


def test_a_dropped_connection_is_retried(dem_service) -> None:
    """The real service resets connections under a burst; a tile is worth retrying."""
    fake = dem_service(fail_times=2)
    grid, _ = elevation_api.fetch_grid(*bbox_around(HILL_LAT, HILL_LON, 1500.0), resolution=64)
    assert np.isfinite(grid.z).all()
    assert fake.tile_requests > fake.fail_times


def test_a_slow_point_lookup_is_retried(dem_service) -> None:
    """The pre-flight check gates every analysis, so it cannot be the weak link."""
    fake = dem_service(point_fail_times=2)
    grid, _ = elevation_api.fetch_grid(*bbox_around(HILL_LAT, HILL_LON, 1500.0), resolution=64)
    assert np.isfinite(grid.z).all()
    assert fake.point_requests > fake.point_fail_times


def test_area_with_no_data_is_refused(dem_service) -> None:
    """Nothing there at all: caught by the point lookup, before any download."""
    fake = dem_service(surface=no_data)
    with pytest.raises(ElevationServiceError, match="No elevation data"):
        elevation_api.fetch_grid(*bbox_around(0.0, -30.0, 2000.0), resolution=64)
    assert fake.tile_requests == 0


def test_tiles_with_no_data_are_refused(dem_service) -> None:
    """And caught again below the point lookup, for coverage that ends mid-area."""
    dem_service(surface=no_data)
    south, west, north, east = bbox_around(0.0, -30.0, 2000.0)
    with pytest.raises(ElevationServiceError, match="no terrain data"):
        elevation_api.sample_area(south, west, north, east, rows=64, cols=64, cell_size_m=30.0)


def test_open_water_is_refused(dem_service) -> None:
    """The seabed has relief and hollows, and is not where a farm pond goes.

    Ocean bathymetry is merged into the same dataset as land elevation, so without
    this check an area over water returns a confident pond site 3 km down.
    """
    fake = dem_service(surface=seabed)
    with pytest.raises(ElevationServiceError, match="is water"):
        elevation_api.fetch_grid(*bbox_around(0.0, -30.0, 3000.0), resolution=64)
    assert fake.tile_requests == 0, "the refusal should cost one point lookup, not a download"


def test_land_below_sea_level_is_still_land(dem_service) -> None:
    """The Dead Sea shore is 400 m down and perfectly good ground.

    Which is why the water test asks the service what the surface *is*, rather
    than assuming a negative elevation means the sea.
    """
    dem_service(surface=below_sea_level)
    grid, _ = elevation_api.fetch_grid(*bbox_around(31.5, 35.5, 2000.0), resolution=64)
    assert float(grid.z.mean()) < 0.0


def test_featureless_terrain_is_refused(dem_service) -> None:
    """Water cannot run down ground with no relief, so it is not worth analysing."""
    dem_service(surface=flat_ground(12.0))
    with pytest.raises(ElevationServiceError, match="flat"):
        elevation_api.fetch_grid(*bbox_around(52.0, 5.0, 2000.0), resolution=64)


def test_an_empty_box_is_refused(service: FakeService) -> None:
    with pytest.raises(ElevationServiceError, match="no extent"):
        elevation_api.fetch_grid(21.25, 81.30, 21.25, 81.30, resolution=64)


def test_point_elevation_reads_the_service(service: FakeService) -> None:
    reading = elevation_api.point_elevation(HILL_LAT, HILL_LON)
    assert reading["elevation_m"] == pytest.approx(HILL_BASE_M + HILL_HEIGHT_M, abs=1.0)
    assert reading["surface_type"] == "land"
    assert service.point_requests == 1


# --------------------------------------------------------------------------- #
# Finding a place by name                                                      #
# --------------------------------------------------------------------------- #

def test_place_search_returns_coordinates(service: FakeService) -> None:
    found = elevation_api.search_places("durg")
    assert [p["name"] for p in found][:2] == [
        "Durg, Chhattisgarh, India",
        "Durg, Durg Tahsil, Chhattisgarh, 491002, India",
    ]
    assert found[0]["latitude"] == pytest.approx(21.1983)
    assert found[0]["kind"] == "administrative"
    assert service.search_requests == 1


def test_place_search_drops_results_with_no_coordinates(service: FakeService) -> None:
    """Nominatim is third-party data; a result we cannot map is not a result."""
    assert all(p["latitude"] is not None for p in elevation_api.search_places("durg"))
    assert not any("Durgapur" in p["name"] for p in elevation_api.search_places("durg"))


def test_place_search_honours_the_limit(service: FakeService) -> None:
    assert len(elevation_api.search_places("durg", limit=1)) == 1


def test_no_match_is_an_empty_list_not_a_failure(service: FakeService) -> None:
    assert elevation_api.search_places("nowhere at all") == []


# --------------------------------------------------------------------------- #
# The live service                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    os.getenv("ELEVATION_LIVE_TESTS") != "1",
    reason="set ELEVATION_LIVE_TESTS=1 to call the real OpenZenith service",
)
def test_live_service_agrees_with_the_sample_contour_map() -> None:
    """The service and the surveyed contours must describe the same hillside.

    ``contours_1m.kml`` says the ground it covers runs from 267 m to 298 m. If
    the downloaded terrain for the same footprint says something else, the
    integration is reading the wrong ground.
    """
    grid, source = elevation_api.fetch_grid(
        21.2398224433387, 81.2814044952393, 21.2635806472203, 81.3126468658447, resolution=120
    )
    assert float(grid.z.min()) == pytest.approx(267.0, abs=3.0)
    assert float(grid.z.max()) == pytest.approx(298.0, abs=3.0)
    assert source.tiles > 0
