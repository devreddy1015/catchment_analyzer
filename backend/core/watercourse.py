"""Separate ground a farm pond can be built on from water that is already there.

A pond is a hole that holds runoff. A river is water passing through, and a large
nala is runoff on its way to one. Neither is pond ground, and the structures they
take — a nala bund with a waste weir, a check dam with a spillway, a minor
irrigation tank with a state clearance — are different engineering under
different approvals.

The distinction terrain can actually answer is size. Every cell knows how much
land drains through it, and upstream area in hectares is the number Indian
watershed practice already uses to choose between these structures. So the
classes here are cut by area:

    ``FARM_POND``   below FARM_POND_MAX_CATCHMENT_HA — a pond may be sited here
    ``NALA``        up to RIVER_CATCHMENT_HA — a bund or percolation tank, not a pond
    ``RIVER``       above that, plus its floodplain — no structure is proposed here

Two things this deliberately does not do.

It does not exempt a channel for being deep. A wide river reads as a *deep*
trench in an elevation model: the sensor sees the water surface rather than the
bed, and noise, bridges and bank canopy all cut into it. A rule of "high flow but
shallow" therefore exempts precisely the rivers it exists to catch, and the
deeper and more river-like the reading, the more certain the exemption.

It does not use a percentile of the map. A percentile calls a fixed share of
*every* map a channel, so a map lying wholly inside one river basin still
nominates the river bed, while a map with no stream on it still discards good
ground. Hectares mean the same thing on every map.

What no elevation model can tell you is whether a channel runs all year: a
perennial river and a monsoon nala are the same trench in a DEM, and only the
flow regime separates them. The classes below are cut by size, which is
conservative and checkable, and :class:`Watercourses` reports the thresholds it
used so the answer can be argued with.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from . import hydrology
from .grid import SMOOTH_WINDOW, ElevationGrid

FARM_POND = "farm_pond"
NALA = "nala"
RIVER = "river"
TRUNCATED = "truncated"
FLOODPLAIN = "floodplain"
STILL_WATER = "still_water"

# A farm pond is sized to its catchment. The ICAR dryland manuals and the MGNREGA
# works schedule put a farm pond's catchment at roughly 1-10 ha; 25 ha is already
# generous for one. Past that a storm delivers more than the pond can keep and
# the water has to be passed on rather than held, which is a spillway problem —
# a nala bund or a percolation tank. Above 100 ha it is a minor irrigation tank
# or a check dam on a named stream: a designed spillway, a sediment regime, and
# in most states the irrigation department's clearance. Nothing that can be
# proposed from a 30 m elevation model.
FARM_POND_MAX_CATCHMENT_HA = 25.0
RIVER_CATCHMENT_HA = 100.0

# Ground kept clear of a river. The trench a DEM shows is already too narrow —
# it is the water surface, not the bed — and the ground immediately beside a
# river is its floodplain, which is not pond ground either. Metres rather than
# cells, so the buffer is the same width whatever resolution was asked for.
RIVER_BUFFER_M = 40.0
NALA_BUFFER_M = 0.0

# How far above the water a pond has to sit, measured as height above nearest
# drainage (see :func:`hydrology.height_above_drainage`). Distance from the
# channel is the wrong question — ground fifty metres from a river but eight
# metres above it is dry, and ground three hundred metres away but level with it
# is riverbed in every monsoon.
#
# This is what a buffer in metres cannot do. A river's floodplain is as wide as
# the valley floor, not as wide as a number: on the sample survey the channel the
# flow model found was 140 cells, while the flat ground lying within a metre of
# its level — visibly the river corridor, on the imagery — was a fifth of the
# map, and every pond site the scoring picked was inside it. Nothing was on the
# blue line; everything was at the blue line's elevation.
#
# Two heights, because two sizes of channel flood differently. A river spreads
# across its whole valley floor and a few metres of it; a nala fills its own bed
# and its banks. Both are ground the water uses and a pond cannot have.
RIVER_FLOODPLAIN_HAND_M = 3.0
NALA_BANK_HAND_M = 1.0

# The catchment of a channel that runs off the edge of the map is not a
# measurement. Flow accumulation counts cells, and it can only count the cells it
# has, so water arriving from outside the window is invisible: a river draining
# three hundred thousand square kilometres reads as two hundred hectares if the
# window is three kilometres across. Measured on four major Indian rivers, the
# largest in-window catchment was 173-391 ha, and along the Godavari's own trunk
# only a third of the cells cleared the river threshold — the rest read as nala,
# and some as farm-pond ground, in the middle of the river.
#
# What the map does know is where the water comes from. Follow a channel upstream
# and it ends either on a hilltop, where the reading is a real catchment, or at
# the boundary, where the reading is a lower bound and the true figure is
# unknowable from this window. The second kind is called truncated: no structure
# is proposed on it, because nothing can be sized against a number that is only a
# floor. Any channel large enough to matter is tested, not just the ones already
# over the river threshold, since a small window truncates small channels too.
TRUNCATION_TEST_MIN_HA = FARM_POND_MAX_CATCHMENT_HA

# A tank, lake or reservoir is a plateau in a DEM: flat to the limit of what the
# source can express, level across its whole extent, and larger than any farm
# pond. Flat farmland passes the first test and is good pond ground, so the other
# two do the work — a field still has a gradient across it, and anything small
# enough to be a pond is left alone.
STILL_WATER_MAX_SLOPE_DEG = 0.5
STILL_WATER_MIN_AREA_HA = 2.0
STILL_WATER_MIN_LEVEL_M = 0.5

# How far a water body may be grown past its flat core to pick up the shoreline
# ramp. Bounded in cells, not just in height: the ramp is an artefact of the
# smoothing window, so it is about that wide, and a height limit alone will walk
# a whole hillside when the source has a coarse vertical step. On a 5 m contour
# survey an unbounded growth swallowed the entire map.
STILL_WATER_SHORE_CELLS = SMOOTH_WINDOW // 2 + 1


@dataclass(frozen=True)
class Watercourses:
    """Which cells are water or watercourse, and why."""

    upstream_ha: np.ndarray      # land draining through each cell, hectares
    river: np.ndarray            # bool: river channel and the buffer beside it
    river_core: np.ndarray       # bool: the channel itself, without the buffer
    truncated: np.ndarray        # bool: channel fed from off the map, catchment unknown
    floodplain: np.ndarray       # bool: ground standing at the water's own level
    height_above_drainage: np.ndarray   # metres above the drainage line each cell drains into
    nala: np.ndarray             # bool: drainage line too large for a farm pond
    still_water: np.ndarray      # bool: standing water already on the ground
    farm_pond_max_ha: float = FARM_POND_MAX_CATCHMENT_HA
    river_min_ha: float = RIVER_CATCHMENT_HA
    buffer_m: float = RIVER_BUFFER_M

    @property
    def excluded(self) -> np.ndarray:
        """Every cell a farm pond may not be sited on."""
        return self.river | self.floodplain | self.nala | self.still_water

    @property
    def blocked(self) -> np.ndarray:
        """Cells where no structure at all is proposed: river, floodplain, open water."""
        return self.river | self.floodplain | self.still_water

    def classify(self, row: int, col: int) -> str:
        """What a place is. Most specific answer first: a nala is a nala, not
        merely the floodplain it necessarily lies in."""
        if self.still_water[row, col]:
            return STILL_WATER
        if self.truncated[row, col]:
            return TRUNCATED
        if self.river[row, col]:
            return RIVER
        if self.nala[row, col]:
            return NALA
        if self.floodplain[row, col]:
            return FLOODPLAIN
        return FARM_POND

    def share(self, mask: np.ndarray) -> float:
        """Fraction of the map a mask covers."""
        return float(mask.mean())

    def summary(self, cell_area_m2: float) -> dict[str, float]:
        """Ground given to each class, in hectares — what the warnings quote.

        The classes overlap on the ground (a nala runs along the bottom of the
        floodplain it made), so each figure counts only what the more specific
        classes above it have not already claimed. That way they add up to the
        total withheld instead of over-reporting it.
        """
        per_ha = cell_area_m2 / 10_000.0
        claimed = self.river | self.still_water
        floodplain_only = self.floodplain & ~claimed & ~self.nala
        return {
            "river_hectares": round(float(self.river.sum()) * per_ha, 2),
            "truncated_hectares": round(float(self.truncated.sum()) * per_ha, 2),
            "floodplain_hectares": round(float(floodplain_only.sum()) * per_ha, 2),
            "nala_hectares": round(float((self.nala & ~claimed).sum()) * per_ha, 2),
            "still_water_hectares": round(float((self.still_water & ~self.river).sum()) * per_ha, 2),
            "excluded_fraction": round(self.share(self.excluded), 4),
        }


def _buffer(mask: np.ndarray, metres: float, cell_size_m: float) -> np.ndarray:
    """Widen a mask by a ground distance, rounded to whole cells."""
    steps = int(round(metres / max(cell_size_m, 1e-6)))
    if steps < 1 or not mask.any():
        return mask
    return ndimage.binary_dilation(mask, ndimage.generate_binary_structure(2, 2), iterations=steps)


def _still_water(grid: ElevationGrid) -> np.ndarray:
    """Connected flat, level patches too large to be anything but standing water.

    Found from the flat core outwards. The core is the easy part — open water is
    the flattest thing in any terrain model — but its edge is not a step. A
    shoreline comes through interpolation and a smoothing filter as a ramp a cell
    or two wide, which is too steep to be called flat and too low to be called
    ground, and it is left as an ordinary-looking fringe lying at the water's own
    level. On the Godavari that fringe was 443 cells of open river still being
    offered as somewhere to dig a pond. So each patch is grown outwards to
    wherever the ground first rises above what the source could distinguish from
    the water level.
    """
    level_step = max(grid.vertical_resolution_m, STILL_WATER_MIN_LEVEL_M)
    min_cells = max(4, int(round(STILL_WATER_MIN_AREA_HA * 10_000.0 / grid.cell_area_m2)))

    flat = grid.slope_deg < STILL_WATER_MAX_SLOPE_DEG
    labels, count = ndimage.label(flat, structure=ndimage.generate_binary_structure(2, 2))
    if count == 0:
        return np.zeros(grid.shape, dtype=bool)

    index = np.arange(1, count + 1)
    sizes = ndimage.sum_labels(np.ones_like(labels, dtype=np.int64), labels, index)
    levels = ndimage.maximum(grid.z, labels, index)
    spread = levels - ndimage.minimum(grid.z, labels, index)

    keep = np.flatnonzero((sizes >= min_cells) & (spread <= level_step)) + 1
    if not keep.size:
        return np.zeros(grid.shape, dtype=bool)

    neighbourhood = ndimage.generate_binary_structure(2, 2)
    water = np.zeros(grid.shape, dtype=bool)
    for label in keep:
        core = labels == label
        shoreline = grid.z <= levels[label - 1] + level_step
        water |= ndimage.binary_dilation(
            core, structure=neighbourhood, iterations=STILL_WATER_SHORE_CELLS, mask=shoreline,
        )

    return water


def _on_boundary(cell: tuple[int, int], shape: tuple[int, int]) -> bool:
    row, col = cell
    return row in (0, shape[0] - 1) or col in (0, shape[1] - 1)


def _truncated_channels(grid: ElevationGrid, accumulation: np.ndarray, direction: np.ndarray,
                        upstream_ha: np.ndarray) -> np.ndarray:
    """Channels whose water arrives from outside the map, so their size is unknown.

    Every channel is walked up its own trunk to wherever the water starts. A trunk
    that ends on a hilltop inside the map has a catchment that was actually
    measured. A trunk that ends at the boundary has not: the reading is whatever
    happened to fit inside the window, and the river carries on regardless.

    Only the upstream ends are walked — a cell whose largest feeder is itself a
    channel is in the middle of one, and its trunk is the same trunk — so the cost
    is one walk per watercourse, not one per cell.
    """
    channel = upstream_ha >= TRUNCATION_TEST_MIN_HA
    if not channel.any():
        return np.zeros(grid.shape, dtype=bool)

    rows, cols = grid.shape
    contributors = hydrology._contributors(direction)
    flat_accumulation = accumulation.ravel()

    heads = []
    for flat in np.flatnonzero(channel.ravel()):
        feeders = contributors.get(int(flat))
        if not feeders or not channel.flat[max(feeders, key=lambda i: flat_accumulation[i])]:
            heads.append(divmod(int(flat), cols))

    truncated = np.zeros(grid.shape, dtype=bool)
    for head in heads:
        trunk = hydrology.trunk_upstream(accumulation, direction, head)
        if _on_boundary(trunk[-1], (rows, cols)):
            for cell in trunk:
                truncated[cell] = True

    return truncated


def classify(grid: ElevationGrid, accumulation: np.ndarray,
             direction: np.ndarray) -> Watercourses:
    """Split the map into pond ground, nala, river, floodplain and open water.

    Two independent questions, and both have to be asked. *How much drains
    through here* separates a farm pond's catchment from a nala's from a river's.
    *How far above the water does this stand* separates the valley floor from the
    shoulder above it — and it is the one that was missing, because a pond in the
    floodplain is not on the channel at all, it is merely at the channel's level,
    which every monsoon settles in its own way.
    """
    upstream_ha = accumulation.astype(float) * grid.cell_area_m2 / 10_000.0

    measured_river = upstream_ha >= RIVER_CATCHMENT_HA
    nala_net = upstream_ha >= FARM_POND_MAX_CATCHMENT_HA

    # A channel fed from off the map is treated as a river, because the only
    # honest thing to say about its size is that it is at least this and probably
    # far more. It is kept as its own mask so the response can say which it is.
    truncated = _truncated_channels(grid, accumulation, direction, upstream_ha)
    river_net = measured_river | truncated

    river = _buffer(river_net, RIVER_BUFFER_M, grid.cell_size_m)

    # A river spreads over its valley floor; a nala fills its bed and its banks.
    above_river = hydrology.height_above_drainage(grid.z, direction, river_net)
    above_nala = hydrology.height_above_drainage(grid.z, direction, nala_net)

    # The test is for ground standing at the water's level, so it cannot be
    # applied to the water's own channel: a drainage line is level with its own
    # continuation downstream by construction, and measuring a nala against the
    # river it runs into always returns nearly nothing. What is being looked for
    # is the *valley floor beside* the channel, so the channels come out first.
    at_water_level = (above_river < RIVER_FLOODPLAIN_HAND_M) | (above_nala < NALA_BANK_HAND_M)
    floodplain = at_water_level & ~river & ~nala_net

    # A bund is built in the channel, so a nala keeps its own bed — but not a bed
    # that is really a river's, nor one whose catchment was never measured.
    nala = _buffer(nala_net, NALA_BUFFER_M, grid.cell_size_m) & ~river

    water = _still_water(grid)

    # The nala network contains the river network, so the channel it finds is
    # always the nearer of the two: this is the tighter of the two heights.
    return Watercourses(
        upstream_ha=upstream_ha,
        river=river,
        river_core=river_net,
        truncated=truncated,
        floodplain=floodplain,
        height_above_drainage=above_nala,
        nala=nala,
        # Kept whole rather than cut back by the river mask: a lake fed by a big
        # enough channel would otherwise stop being a lake, and `classify` already
        # decides which name a cell gets while `summary` handles the overlap.
        still_water=water,
    )
