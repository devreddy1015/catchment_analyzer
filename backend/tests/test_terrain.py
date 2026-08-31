"""Grid reconstruction and D8 hydrology."""
from __future__ import annotations

import numpy as np
import pytest

from backend.core import hydrology, kml, siting
from backend.core.grid import ElevationGrid, build_grid


@pytest.fixture(scope="module")
def hill_grid(synthetic_kml: bytes):
    return build_grid(kml.read_contours(synthetic_kml), resolution=80)


def test_grid_cells_are_near_square(hill_grid) -> None:
    """Rows and cols follow the ground aspect ratio, so a cell is square in metres."""
    from backend.core.geo import haversine_m

    rows, cols = hill_grid.shape
    mid_lat = (hill_grid.north + hill_grid.south) / 2
    width_m = haversine_m(mid_lat, hill_grid.west, mid_lat, hill_grid.east)
    height_m = haversine_m(hill_grid.south, hill_grid.west, hill_grid.north, hill_grid.west)

    assert width_m / cols == pytest.approx(height_m / rows, rel=0.05)


def test_grid_reproduces_contour_elevations(synthetic_kml: bytes, hill_grid) -> None:
    """Interpolation must stay inside the range the contours actually described."""
    low, high = kml.read_contours(synthetic_kml).elevation_range
    assert low - 1 <= hill_grid.z.min() <= hill_grid.z.max() <= high + 1


def test_grid_is_north_up(hill_grid) -> None:
    assert hill_grid.lats[0] > hill_grid.lats[-1]
    assert hill_grid.lons[0] < hill_grid.lons[-1]


def test_cell_and_point_round_trip(hill_grid) -> None:
    lat, lon = hill_grid.point_at(7, 11)
    assert hill_grid.cell_at(lat, lon) == (7, 11)


def test_cell_lookup_clamps_outside_the_grid(hill_grid) -> None:
    rows, cols = hill_grid.shape
    assert hill_grid.cell_at(90.0, -180.0) == (0, 0)
    assert hill_grid.cell_at(-90.0, 180.0) == (rows - 1, cols - 1)


def test_fill_sinks_raises_a_pit_to_its_rim() -> None:
    z = np.full((5, 5), 10.0)
    z[2, 2] = 4.0
    filled = hydrology.fill_sinks(z)

    assert filled[2, 2] == pytest.approx(10.0)
    assert filled[0, 0] == pytest.approx(10.0)


def test_depression_depth_measures_the_hollow() -> None:
    z = np.full((5, 5), 10.0)
    z[2, 2] = 4.0
    depths = hydrology.depression_depth(z)

    assert depths[2, 2] == pytest.approx(6.0)
    assert depths[0, 0] == pytest.approx(0.0)


def test_flow_runs_downhill() -> None:
    """On a plane tilted down to the east, every cell must point east."""
    z = np.tile(np.linspace(100.0, 0.0, 10), (6, 1))
    direction = hydrology.flow_direction(z, cell_size_m=10.0)

    east = hydrology.D8_OFFSETS.index((0, 1))
    assert (direction[:, :-1] == east).all()
    assert (direction[:, -1] == -1).all()  # nowhere lower to go at the low edge


def test_accumulation_totals_every_cell() -> None:
    """All flow on a tilted plane ends at the low edge, so the column sums to the map."""
    z = np.tile(np.linspace(100.0, 0.0, 10), (6, 1))
    direction = hydrology.flow_direction(z, cell_size_m=10.0)
    accumulation = hydrology.flow_accumulation(z, direction)

    assert accumulation.min() >= 1
    assert accumulation[:, -1].sum() == z.size


def test_conditioned_surface_drains_a_pit() -> None:
    """Routing on the raw grid strands water in a pit; conditioning must not."""
    z = np.tile(np.linspace(100.0, 0.0, 12), (12, 1))
    z[6, 6] -= 20.0

    raw = hydrology.flow_direction(z, 10.0)
    conditioned = hydrology.flow_direction(hydrology.conditioned_surface(z), 10.0)

    assert raw[6, 6] == -1
    assert conditioned[6, 6] != -1


def test_catchment_of_a_cone_is_bounded_and_uphill(hill_grid) -> None:
    routed = hydrology.conditioned_surface(hill_grid.z)
    direction = hydrology.flow_direction(routed, hill_grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)

    outlet = np.unravel_index(accumulation.argmax(), accumulation.shape)
    catchment = hydrology.delineate(hill_grid, direction, (int(outlet[0]), int(outlet[1])))

    assert 0 < len(catchment.cells) <= hill_grid.z.size
    assert catchment.area_m2 == pytest.approx(len(catchment.cells) * hill_grid.cell_area_m2)
    assert catchment.boundary[0] == catchment.boundary[-1]  # closed ring
    assert catchment.max_elevation_m >= hill_grid.z[outlet]  # drains from above
    assert catchment.flow_path_length_m > 0


def test_storage_never_exceeds_the_spill_level(hill_grid) -> None:
    depths = hydrology.depression_depth(hill_grid.z)
    routed = hydrology.conditioned_surface(hill_grid.z)
    direction = hydrology.flow_direction(routed, hill_grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)

    for site in siting.rank_sites(hill_grid, accumulation, direction, depths, max_sites=3):
        assert site.storage.spill_elevation_m >= site.elevation_m
        assert site.storage.volume_m3 >= 0
        assert 0 <= site.score <= 100


def test_sites_are_spaced_apart(hill_grid) -> None:
    depths = hydrology.depression_depth(hill_grid.z)
    routed = hydrology.conditioned_surface(hill_grid.z)
    direction = hydrology.flow_direction(routed, hill_grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)

    sites = siting.rank_sites(hill_grid, accumulation, direction, depths, max_sites=4)
    separation = max(3, min(hill_grid.shape) // 8)

    for i, a in enumerate(sites):
        for b in sites[i + 1:]:
            assert (a.row - b.row) ** 2 + (a.col - b.col) ** 2 >= separation ** 2
    assert [s.rank for s in sites] == sorted(s.rank for s in sites)
    assert [s.score for s in sites] == sorted((s.score for s in sites), reverse=True)


# --------------------------------------------------------------------------- #
# Telling a pond site from a place in a watercourse                            #
# --------------------------------------------------------------------------- #

def channel_terrain(vertical_resolution_m: float) -> tuple[ElevationGrid, np.ndarray, np.ndarray, np.ndarray]:
    """A valley with a stream down it, and one real pond-sized hollow beside it.

    The valley floor dips by half a metre here and there — the size of wobble
    smoothing leaves behind on a whole-metre DEM, and the size that was actually
    getting recommended over real terrain. It is deep enough to clear a fixed
    30 cm bar and far too shallow to be a measurement, which is the whole point:
    a rule that trusts any dip at all recommends the middle of the stream.
    """
    rows = cols = 80
    y, x = np.mgrid[0:rows, 0:cols]

    # A valley running north to south, its floor falling gently southward.
    surface = 100.0 + 0.28 * np.abs(x - cols / 2) - 0.05 * y

    # Interpolation wobble along the channel floor: far too shallow to be real.
    floor = np.abs(x - cols / 2) < 2
    surface[floor] -= 0.45 * np.sin(y[floor] * 0.9) + 0.45

    # A genuine hollow well off the channel, two metres deep.
    pocket = (y - 30) ** 2 + (x - 62) ** 2 < 36
    surface[pocket] -= 2.0

    grid = ElevationGrid(
        z=surface, south=0.0, west=0.0, north=0.02, east=0.02, cell_size_m=20.0,
        vertical_resolution_m=vertical_resolution_m,
    )
    routed = hydrology.conditioned_surface(grid.z)
    direction = hydrology.flow_direction(routed, grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)
    return grid, direction, accumulation, hydrology.depression_depth(grid.z)


def test_the_stream_bed_is_not_offered_as_a_pond_site() -> None:
    """A dip smaller than the data's own resolution is not a hollow.

    The DEM is whole metres, so a half-metre wobble on a busy drainage line came
    out of interpolation, not out of the ground. Recommending it means telling
    someone to build a farm pond in a watercourse.

    The claim is about flow, not about geometry: the head of the valley is on the
    same line but carries almost nothing, and a hollow there is a hollow. What
    must never be offered is a cell that a lot of water runs through and that has
    no measurable hollow to keep any of it.
    """
    grid, direction, accumulation, depths = channel_terrain(vertical_resolution_m=1.0)
    sites = siting.rank_sites(grid, accumulation, direction, depths, max_sites=5)
    assert sites, "there is a real hollow here, so something should be found"

    carries_flow = np.percentile(accumulation, siting.CHANNEL_FLOW_PERCENTILE)
    in_watercourse = [
        (s.rank, s.row, s.col, s.upstream_cells, s.depression_depth_m)
        for s in sites
        if s.upstream_cells >= carries_flow and s.depression_depth_m < grid.vertical_resolution_m
    ]
    assert not in_watercourse, f"sites placed in the watercourse: {in_watercourse}"


def test_the_real_hollow_beside_the_stream_is_found() -> None:
    grid, direction, accumulation, depths = channel_terrain(vertical_resolution_m=1.0)
    best = siting.rank_sites(grid, accumulation, direction, depths, max_sites=3)[0]
    assert abs(best.row - 30) <= 6 and abs(best.col - 62) <= 6
    assert best.depression_depth_m > 1.0


def test_a_finer_survey_is_allowed_to_trust_a_finer_hollow() -> None:
    """The bar is the terrain's resolution, not a number chosen once.

    A 10 cm survey can see a 12 cm hollow, so the same ground read more finely is
    permitted to consider what a whole-metre DEM must not.
    """
    coarse = channel_terrain(vertical_resolution_m=1.0)
    fine = channel_terrain(vertical_resolution_m=0.1)

    def channel_cells(bundle) -> int:
        grid, direction, accumulation, depths = bundle
        holds = max(siting.CHANNEL_MIN_DEPRESSION_M, grid.vertical_resolution_m)
        busy = accumulation >= np.percentile(accumulation, siting.CHANNEL_FLOW_PERCENTILE)
        return int((busy & (depths < holds) & (direction >= 0)).sum())

    assert channel_cells(coarse) > channel_cells(fine)


def test_a_hollow_one_storm_would_overtop_is_called_a_nala_bund() -> None:
    """Storage smaller than the storm above it is a spillway problem, and says so."""
    small = siting.StorageEstimate(depth_m=1.0, spill_elevation_m=100.0,
                                   surface_area_m2=400.0, volume_m3=200.0)
    parts = {"depression": 0.5, "catchment": 0.9, "slope": 0.8, "elevation": 0.7}

    storm = siting._storm_inflow_m3(upstream_cells=3000, cell_area_m2=400.0)
    assert storm > small.volume_m3
    assert any("nala bund" in r for r in siting._describe(parts, 1.0, small, 120.0, storm))

    # The same hollow under a catchment small enough to fill it slowly is a pond.
    quiet = siting._storm_inflow_m3(upstream_cells=2, cell_area_m2=400.0)
    assert not any("nala bund" in r for r in siting._describe(parts, 1.0, small, 0.08, quiet))
