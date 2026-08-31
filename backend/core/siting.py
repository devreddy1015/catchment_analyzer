"""Pick where a pond should go, using only what the terrain says.

Every cell is scored on four things a contour map can actually answer:

    depression  how much natural hollow already exists there
    catchment   how much land drains through it
    slope       how flat the ground is, which sets excavation cost
    elevation   how low it sits, so water arrives by gravity

The weighted sum becomes a 0-100 score. Cells that are really an active stream
channel — a lot of flow passing through but no hollow to hold it — are removed
first: damming one is a check dam, a different structure with different
engineering, not a farm pond. Sites are then taken best-first while keeping
them spaced apart, so the result is a set of genuine alternatives rather than a
cluster of neighbouring cells.

No thresholds here are tied to a particular map: scores are normalised against
the range present in whatever terrain was uploaded.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .grid import ElevationGrid

# Score weights; they are normalised, so relative size is what matters.
WEIGHTS = {"depression": 0.30, "catchment": 0.30, "slope": 0.25, "elevation": 0.15}

# Slope at which the slope score has fallen to half. Above this, ground is
# steep enough that excavation and embankment costs climb sharply.
SLOPE_HALF_SCORE_DEG = 8.0

# A cell counts as an active channel when its upstream area is in the top
# CHANNEL_FLOW_PERCENTILE of the map yet holds too little standing depth to be
# storage. Damming one of these is a check dam or a weir — a different structure,
# with a spillway, a sediment problem and usually a permit — not a farm pond.
#
# "Too little" cannot be a fixed number of centimetres. A hollow shallower than
# the source could record is not a shallow hollow, it is interpolation between
# two identical readings, and on a busy channel that is exactly what a smoothing
# filter manufactures. So the bar is the coarser of a floor and the terrain's own
# vertical resolution: the contour interval, or one metre for integer-metre DEM
# tiles. Below that the map simply does not know whether anything is there.
CHANNEL_FLOW_PERCENTILE = 95.0
CHANNEL_MIN_DEPRESSION_M = 0.30

# A hollow that one ordinary storm fills and overtops is not storing water, it is
# passing it on, and what has to be built there is a spillway rather than an
# embankment. 50 mm in a day is an ordinary storm across monsoon India, and the
# rational method below is the same one the response already uses for yield, so
# the comparison is like for like rather than a second invented rule.
STORM_TEST_MM = 50.0
STORM_TEST_RUNOFF_C = 0.4

RATINGS = ((70.0, "Excellent"), (55.0, "Good"), (40.0, "Moderate"), (0.0, "Marginal"))


@dataclass
class StorageEstimate:
    """Water the natural hollow at this site holds before it spills over."""

    depth_m: float
    spill_elevation_m: float
    surface_area_m2: float
    volume_m3: float


@dataclass
class PondSite:
    rank: int
    row: int
    col: int
    latitude: float
    longitude: float
    elevation_m: float
    slope_deg: float
    depression_depth_m: float
    upstream_cells: int
    score: float
    rating: str
    component_scores: dict[str, float]
    storage: StorageEstimate
    reasons: list[str] = field(default_factory=list)


def _normalise(values: np.ndarray) -> np.ndarray:
    """Rescale to 0-1 against the range actually present in this terrain."""
    low, high = float(values.min()), float(values.max())
    return (values - low) / (high - low) if high > low else np.zeros_like(values)


def natural_storage(grid: ElevationGrid, depths: np.ndarray, cell: tuple[int, int]) -> StorageEstimate:
    """How much water the hollow containing this site holds before it overflows.

    Sink filling already gives the answer for the depth: the fill added at a cell
    is exactly the head of water standing there, so the rim it spills over sits at
    ``elevation + fill``. Growing the pond outwards from the site up to that rim
    gives the surface area and volume. Because the rim is a real feature of the
    terrain, the pond cannot run away across the map the way a fixed design depth
    would; a site with no hollow simply reports zero and has to be dug out.
    """
    rows, cols = grid.shape
    ground = float(grid.z[cell])
    spill = ground + float(depths[cell])

    if depths[cell] <= 0.0:
        return StorageEstimate(0.0, round(ground, 2), 0.0, 0.0)

    flooded = {cell}
    queue = deque([cell])
    head_sum = 0.0
    while queue:
        r, c = queue.popleft()
        head_sum += spill - float(grid.z[r, c])
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in flooded:
                if grid.z[nr, nc] < spill:
                    flooded.add((nr, nc))
                    queue.append((nr, nc))

    return StorageEstimate(
        depth_m=round(float(depths[cell]), 2),
        spill_elevation_m=round(spill, 2),
        surface_area_m2=round(len(flooded) * grid.cell_area_m2, 1),
        volume_m3=round(head_sum * grid.cell_area_m2, 1),
    )


def _storm_inflow_m3(upstream_cells: int, cell_area_m2: float) -> float:
    """Runoff a single ordinary storm delivers to a site, by the rational method."""
    return upstream_cells * cell_area_m2 * STORM_TEST_RUNOFF_C * STORM_TEST_MM / 1000.0


def _describe(components: dict[str, float], site_depth_m: float, storage: StorageEstimate,
              upstream_ha: float, storm_m3: float) -> list[str]:
    reasons: list[str] = []
    if components["depression"] >= 0.4:
        reasons.append(f"Sits in a natural hollow about {site_depth_m:.1f} m deep, so less digging")
    if components["catchment"] >= 0.6:
        reasons.append("A large upstream area drains through this point")
    if components["slope"] >= 0.6:
        reasons.append("Gentle ground keeps excavation and embankment costs down")
    if components["elevation"] >= 0.6:
        reasons.append("Low-lying, so runoff arrives by gravity")
    if storage.volume_m3 > 0:
        reasons.append(
            f"The hollow fills to {storage.spill_elevation_m:g} m before spilling, covering "
            f"{storage.surface_area_m2:,.0f} m² and holding about {storage.volume_m3:,.0f} m³"
        )

    if components["slope"] < 0.35:
        reasons.append("Caution: the ground is steep compared with the rest of the map")
    if components["depression"] < 0.2:
        reasons.append("Caution: almost no natural hollow — the basin has to be dug")
    if components["catchment"] < 0.3:
        reasons.append("Caution: little land drains here, so inflow will be limited")
    if storage.volume_m3 > 0 and storm_m3 > storage.volume_m3:
        # It cleared the channel test, so the hollow is real — but the land above
        # it delivers more in one storm than the hollow can keep.
        reasons.append(
            f"Caution: {upstream_ha:,.0f} ha drains through here, and 50 mm of rain on it "
            f"yields about {storm_m3:,.0f} m³ against the {storage.volume_m3:,.0f} m³ this "
            f"hollow holds. It fills and spills in one ordinary storm, so this is a nala "
            f"bund needing a spillway, not a farm pond"
        )
    return reasons


def _rating(score: float) -> str:
    return next(label for cutoff, label in RATINGS if score >= cutoff)


def rank_sites(
    grid: ElevationGrid,
    accumulation: np.ndarray,
    direction: np.ndarray,
    depths: np.ndarray,
    max_sites: int = 5,
) -> list[PondSite]:
    """Score the terrain and return the best-spaced candidate pond sites."""
    rows, cols = grid.shape

    depression = _normalise(depths)
    catchment = _normalise(np.log1p(accumulation.astype(float)))
    slope = _normalise(1.0 / (1.0 + grid.slope_deg / SLOPE_HALF_SCORE_DEG))
    elevation = _normalise(-grid.z)

    components = {
        "depression": depression,
        "catchment": catchment,
        "slope": slope,
        "elevation": elevation,
    }
    total_weight = sum(WEIGHTS.values())
    score = sum(WEIGHTS[name] / total_weight * layer for name, layer in components.items()) * 100.0

    # Drop cells that are stream channel rather than storage.
    holds_water = max(CHANNEL_MIN_DEPRESSION_M, grid.vertical_resolution_m)
    channel = (
        (accumulation >= np.percentile(accumulation, CHANNEL_FLOW_PERCENTILE))
        & (depths < holds_water)
        & (direction >= 0)
    )
    score[channel] = 0.0

    # Drop the outer ring: interpolation is least reliable there and flow
    # directions point off the map.
    edge = max(2, min(rows, cols) // 40)
    score[:edge, :] = score[-edge:, :] = 0.0
    score[:, :edge] = score[:, -edge:] = 0.0

    # Best first, keeping candidates apart. Ties break on catchment, then depth,
    # then position, so the same input always yields the same output.
    min_separation = max(3, min(rows, cols) // 8)
    order = sorted(
        (int(i) for i in np.flatnonzero(score)),
        key=lambda i: (
            -score.flat[i],
            -accumulation.flat[i],
            -depths.flat[i],
            grid.z.flat[i],
            i,
        ),
    )

    chosen: list[tuple[int, int]] = []
    for flat in order:
        r, c = divmod(flat, cols)
        if all((r - pr) ** 2 + (c - pc) ** 2 >= min_separation ** 2 for pr, pc in chosen):
            chosen.append((r, c))
        if len(chosen) >= max_sites:
            break

    sites: list[PondSite] = []
    for rank, (r, c) in enumerate(chosen, start=1):
        parts = {name: round(float(layer[r, c]), 3) for name, layer in components.items()}
        storage = natural_storage(grid, depths, (r, c))
        upstream_ha = float(accumulation[r, c]) * grid.cell_area_m2 / 10_000.0
        storm_m3 = _storm_inflow_m3(int(accumulation[r, c]), grid.cell_area_m2)
        lat, lon = grid.point_at(r, c)
        site_score = round(float(score[r, c]), 1)
        sites.append(PondSite(
            rank=rank,
            row=r,
            col=c,
            latitude=round(lat, 6),
            longitude=round(lon, 6),
            elevation_m=round(float(grid.z[r, c]), 2),
            slope_deg=round(float(grid.slope_deg[r, c]), 2),
            depression_depth_m=round(float(depths[r, c]), 2),
            upstream_cells=int(accumulation[r, c]),
            score=site_score,
            rating=_rating(site_score),
            component_scores=parts,
            storage=storage,
            reasons=_describe(parts, float(depths[r, c]), storage, upstream_ha, storm_m3),
        ))

    return sites
