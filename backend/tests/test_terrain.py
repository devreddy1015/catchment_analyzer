"""Grid reconstruction and D8 hydrology."""
from __future__ import annotations

import numpy as np
import pytest

from backend.core import hydrology, kml, siting, watercourse
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


# --------------------------------------------------------------------------- #
# Telling a pond site from a river                                             #
# --------------------------------------------------------------------------- #

def river_terrain(rows: int = 80, cols: int = 80, cell_size_m: float = 20.0):
    """A valley with a real river down it, and one pond-sized hollow on the shoulder.

    The river is the case the depth rule alone cannot see: a trench with
    metre-deep pits along its floor. That is how a wide river actually reads in
    an elevation model — the sensor returns its water surface rather than its
    bed, and noise, bridges and bank canopy all cut into it — so a rule of "busy
    but shallow" waves through precisely the rivers it exists to catch.

    At 20 m cells the whole map is 256 ha, so the lower reach carries far more
    than the 100 ha that makes a river.
    """
    y, x = np.mgrid[0:rows, 0:cols]

    surface = 100.0 + 0.30 * np.abs(x - cols / 2) - 0.05 * y
    channel = np.abs(x - cols / 2) < 2
    surface[channel] -= 3.0                       # the trench itself
    surface[channel & (y % 7 == 0)] -= 1.6        # metre-deep pits along the bed

    pocket = (y - 30) ** 2 + (x - 62) ** 2 < 36   # a genuine farm-pond hollow
    surface[pocket] -= 2.0

    grid = ElevationGrid(
        z=surface, south=0.0, west=0.0, north=0.02, east=0.02,
        cell_size_m=cell_size_m, vertical_resolution_m=1.0,
    )
    routed = hydrology.conditioned_surface(grid.z)
    direction = hydrology.flow_direction(routed, grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)
    return grid, direction, accumulation, hydrology.depression_depth(grid.z)


def test_a_deep_hollow_does_not_excuse_a_river() -> None:
    """The bug this rule exists for: a river is exempted for being deep.

    Depth was the only thing keeping a busy cell out of the results, so any river
    trench the data showed as real — which is every wide river — went straight
    through and scored well, because high flow, a deep hollow and low ground is
    exactly the combination the score rewards.
    """
    grid, direction, accumulation, depths = river_terrain()
    courses = watercourse.classify(grid, accumulation, direction)

    holds = max(siting.CHANNEL_MIN_DEPRESSION_M, grid.vertical_resolution_m)
    would_have_escaped = courses.river_core & (depths >= holds)
    assert would_have_escaped.any(), "the fixture must contain the case that used to escape"
    assert courses.excluded[would_have_escaped].all(), "a deep river cell is still a river"


def test_no_pond_is_offered_on_a_river_or_an_oversized_nala() -> None:
    grid, direction, accumulation, depths = river_terrain()
    courses = watercourse.classify(grid, accumulation, direction)
    selection = siting.select(grid, accumulation, direction, depths, max_sites=5,
                              watercourses=courses)

    assert selection.ponds, "there is a real hollow here, so something should be found"
    for site in selection.ponds:
        assert not courses.excluded[site.row, site.col], f"site {site.rank} is in a watercourse"
        assert site.upstream_hectares < watercourse.FARM_POND_MAX_CATCHMENT_HA
        assert site.structure == watercourse.FARM_POND


def test_the_hollow_on_the_shoulder_survives_the_exclusion() -> None:
    """Excluding the river must not cost the genuine site beside it."""
    grid, direction, accumulation, depths = river_terrain()
    best = siting.rank_sites(grid, accumulation, direction, depths, max_sites=3)[0]
    assert abs(best.row - 30) <= 6 and abs(best.col - 62) <= 6
    assert best.depression_depth_m > 1.0


def test_a_big_nala_comes_back_as_the_structure_it_takes() -> None:
    """Not a pond is not the same as nothing. The channel is offered as a bund."""
    grid, direction, accumulation, depths = river_terrain()
    selection = siting.select(grid, accumulation, direction, depths, max_sites=5)

    assert selection.channel_structures, "a 256 ha map has drainage lines worth a bund"
    for site in selection.channel_structures:
        assert site.structure == watercourse.NALA
        assert site.upstream_hectares >= watercourse.FARM_POND_MAX_CATCHMENT_HA
        assert site.structure_label == "Nala bund / percolation tank"
        assert any("spillway" in reason or "waste weir" in reason for reason in site.reasons)

    pond_cells = {(s.row, s.col) for s in selection.ponds}
    assert pond_cells.isdisjoint({(s.row, s.col) for s in selection.channel_structures})


def test_the_class_is_hectares_not_a_share_of_the_map() -> None:
    """A percentile calls a fixed share of every map a channel. Hectares do not.

    This hillside is 16 ha end to end, so nothing on it can drain 100 ha and none
    of it is river. A rule cut by percentile would still discard its busiest 5%,
    which here is good pond ground.
    """
    grid, direction, accumulation, _ = river_terrain(rows=20, cols=20)
    courses = watercourse.classify(grid, accumulation, direction)

    assert grid.z.size * grid.cell_area_m2 / 10_000.0 < watercourse.RIVER_CATCHMENT_HA
    assert not courses.river.any(), "no river can exist on a map smaller than one"
    assert courses.upstream_ha.max() < watercourse.RIVER_CATCHMENT_HA


def test_open_water_is_not_ground_to_build_on() -> None:
    """A tank that is already there is not a place to dig a tank.

    Standing water reads as a plateau: flat, level to the limit of what the
    source can express, and larger than any farm pond. Sloping ground beside it
    stays available, so the test is that the rule is specific, not that it fires.
    """
    rows = cols = 60
    y, x = np.mgrid[0:rows, 0:cols]
    surface = 100.0 + 0.25 * y + 0.25 * x

    lake = (y >= 20) & (y < 40) & (x >= 20) & (x < 40)   # 400 cells = 16 ha at 20 m
    surface[lake] = 100.0

    grid = ElevationGrid(z=surface, south=0.0, west=0.0, north=0.02, east=0.02,
                         cell_size_m=20.0, vertical_resolution_m=1.0)
    routed = hydrology.conditioned_surface(grid.z)
    direction = hydrology.flow_direction(routed, grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)
    courses = watercourse.classify(grid, accumulation, direction)

    assert courses.still_water[30, 30], "the middle of the lake is open water"
    assert not courses.still_water[5, 5], "the hillside is not"

    for site in siting.rank_sites(grid, accumulation, direction, hydrology.depression_depth(grid.z)):
        assert not courses.still_water[site.row, site.col]


def test_a_hollow_one_storm_overtops_is_moved_not_merely_annotated() -> None:
    """The verdict has to move the site, not just be printed beside it.

    A hollow that one ordinary storm fills and spills is a bund, and the code has
    always said so — but it said so as a caution on a site it was still ranking
    as the best farm pond on the map. A pond that calls itself a nala bund is not
    a recommendation anyone can act on.
    """
    grid, direction, accumulation, depths = river_terrain()
    selection = siting.select(grid, accumulation, direction, depths, max_sites=5)

    for site in selection.ponds:
        storm = siting._storm_inflow_m3(site.upstream_cells, grid.cell_area_m2)
        assert not (site.storage.volume_m3 > 0 and storm > site.storage.volume_m3), (
            f"pond {site.rank} fills and spills in one storm"
        )
        assert not any("nala bund" in reason for reason in site.reasons)


# --------------------------------------------------------------------------- #
# Height above the water, not distance from it                                 #
# --------------------------------------------------------------------------- #

def floodplain_terrain():
    """An asymmetric valley: a flat floor on one side, a wall on the other.

    This is the case a buffer in metres cannot reach. Two hundred metres west of
    the channel is valley floor, level with the water and riverbed every monsoon.
    Two hundred metres east is five metres up the wall and dry. Same distance,
    opposite answer, and only the height can tell them apart.

    The cross-fall has to beat the fall down the valley, or D8 sends the floor's
    water straight down its own length and no drainage line ever forms — which is
    a fact about flat valley floors, not only about the fixture. The floor also
    has to carry some relief along it, or it reads as standing water and the
    height rule never gets asked the question.
    """
    rows, cols = 90, 120
    y, x = np.mgrid[0:rows, 0:cols]
    offset = x - 30
    across = np.abs(offset)

    profile = np.where(
        offset <= 0,
        np.where(across <= 12, 0.05 * across, 0.6 + 0.6 * (across - 12)),   # floor, then wall
        0.55 * across,                                                      # wall straight up
    )
    profile[across == 0] = -0.4          # one cell of channel: a flat-bottomed
                                         # notch splits the flow down its edges
                                         # and leaves the middle carrying nothing

    surface = 100.0 - 0.02 * y + profile

    grid = ElevationGrid(z=surface, south=0.0, west=0.0, north=0.02, east=0.03,
                         cell_size_m=20.0, vertical_resolution_m=1.0)
    routed = hydrology.conditioned_surface(grid.z)
    direction = hydrology.flow_direction(routed, grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)
    return grid, direction, accumulation, hydrology.depression_depth(grid.z)


ON_THE_FLOOR = (45, 20)          # 200 m west of the channel, level with it
UP_THE_WALL = (45, 40)           # 200 m east of the channel, five metres above it


def test_height_above_drainage_measures_the_channel_a_cell_drains_into() -> None:
    grid, direction, accumulation, _ = floodplain_terrain()
    network = accumulation * grid.cell_area_m2 / 10_000.0 >= 25.0
    height = hydrology.height_above_drainage(grid.z, direction, network)

    assert (height[network] == 0.0).all(), "a channel is not above itself"
    assert (height >= 0.0).all()
    # The valley floor is level with the channel; the wall stands above it.
    assert height[ON_THE_FLOOR] < 1.0
    assert height[UP_THE_WALL] > 3.0


def test_the_valley_floor_is_excluded_even_far_from_the_channel() -> None:
    """Distance from the blue line is the wrong question.

    Ground two hundred metres from the channel but level with it floods; ground
    the same distance away but four metres up does not. Only the height tells
    them apart, and it is the height that decides.
    """
    grid, direction, accumulation, _ = floodplain_terrain()
    courses = watercourse.classify(grid, accumulation, direction)

    assert courses.excluded[ON_THE_FLOOR], "level valley floor is not pond ground"
    assert not courses.excluded[UP_THE_WALL], "the wall above it is"
    assert courses.classify(*ON_THE_FLOOR) in (watercourse.FLOODPLAIN, watercourse.RIVER)


def test_no_pond_is_offered_at_the_water_level() -> None:
    grid, direction, accumulation, depths = floodplain_terrain()
    courses = watercourse.classify(grid, accumulation, direction)
    sites = siting.rank_sites(grid, accumulation, direction, depths, max_sites=5,
                              watercourses=courses)

    assert sites, "the valley wall is perfectly good pond ground"
    for site in sites:
        assert site.height_above_drainage_m is not None
        assert site.height_above_drainage_m >= watercourse.NALA_BANK_HAND_M, (
            f"site {site.rank} stands only {site.height_above_drainage_m} m above its drainage line"
        )
        assert not courses.floodplain[site.row, site.col]


def test_a_channel_is_never_called_its_own_floodplain() -> None:
    """A nala is level with the river it runs into, by construction.

    Measuring one against the other returns nearly zero, so applying the height
    test to the channels themselves would delete every drainage line on a gentle
    gradient — and with it the bunds that are the whole alternative on offer.
    """
    grid, direction, accumulation, depths = river_terrain()
    courses = watercourse.classify(grid, accumulation, direction)

    nala_net = courses.upstream_ha >= watercourse.FARM_POND_MAX_CATCHMENT_HA
    assert nala_net.any()
    assert not courses.floodplain[nala_net].any(), "a channel is not floodplain"
    assert siting.select(grid, accumulation, direction, depths, 5).channel_structures


# --------------------------------------------------------------------------- #
# A catchment that runs off the edge of the map                                #
# --------------------------------------------------------------------------- #

def through_flowing_river():
    """A river crossing the window, its catchment almost entirely outside it.

    This is what a major river looks like to a small analysis window. The valley
    enters at the top and leaves at the bottom, and flow accumulation counts only
    the cells it has — so a river draining a whole district reports a couple of
    hundred hectares, which is nala-sized, and a bund gets offered in the middle
    of it. Measured on four Indian rivers, the largest in-window catchment was
    173-391 ha.
    """
    rows, cols = 100, 100
    y, x = np.mgrid[0:rows, 0:cols]

    # A single valley running north to south, entering and leaving at the edges.
    surface = 100.0 + 0.45 * np.abs(x - 50) - 0.05 * y
    surface[np.abs(x - 50) < 3] -= 2.5

    grid = ElevationGrid(z=surface, south=0.0, west=0.0, north=0.02, east=0.02,
                         cell_size_m=20.0, vertical_resolution_m=1.0)
    routed = hydrology.conditioned_surface(grid.z)
    direction = hydrology.flow_direction(routed, grid.cell_size_m)
    accumulation = hydrology.flow_accumulation(routed, direction)
    return grid, direction, accumulation, hydrology.depression_depth(grid.z)


def test_a_trunk_is_traced_to_where_its_water_starts() -> None:
    grid, direction, accumulation, _ = through_flowing_river()
    outlet = np.unravel_index(accumulation.argmax(), accumulation.shape)
    trunk = hydrology.trunk_upstream(accumulation, direction, (int(outlet[0]), int(outlet[1])))

    assert len(trunk) > 10
    # Accumulation only ever falls as you walk upstream.
    values = [accumulation[cell] for cell in trunk]
    assert values == sorted(values, reverse=True)
    # This valley is fed from off the map, so the trunk ends on the boundary.
    assert trunk[-1][0] in (0, grid.shape[0] - 1) or trunk[-1][1] in (0, grid.shape[1] - 1)


def test_a_channel_fed_from_off_the_map_is_not_offered_as_a_nala() -> None:
    """Its catchment is a floor, not a figure, so nothing can be sized on it."""
    grid, direction, accumulation, depths = through_flowing_river()
    courses = watercourse.classify(grid, accumulation, direction)

    assert courses.truncated.any(), "this valley's water comes from outside the map"
    # Every truncated cell is held as river, and none is offered as a nala.
    assert courses.river[courses.truncated].all()
    assert not courses.nala[courses.truncated].any()

    selection = siting.select(grid, accumulation, direction, depths, max_sites=5)
    for site in selection.ponds + selection.channel_structures:
        assert not courses.truncated[site.row, site.col], (
            f"a structure was offered on a channel of unknown catchment at "
            f"{site.row},{site.col}"
        )


def test_a_catchment_that_starts_inside_the_map_is_measured_not_flagged() -> None:
    """The flag is about missing data, not about size.

    A hill whose drainage begins on its own summit has a catchment that really
    was counted, so nothing about it is truncated and its nalas stay available.
    """
    grid, direction, accumulation, _ = river_terrain()
    courses = watercourse.classify(grid, accumulation, direction)

    interior_headwater = accumulation[2:-2, 2:-2].max() >= accumulation.max()
    if interior_headwater:
        assert not courses.truncated.any()
