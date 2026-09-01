"""Pick where a pond should go, using only what the terrain says.

Every cell is scored on four things a contour map can actually answer:

    depression  how much natural hollow already exists there
    catchment   how much land drains through it
    slope       how flat the ground is, which sets excavation cost
    elevation   how low it sits, so water arrives by gravity

The weighted sum becomes a 0-100 score. Before any of it is used, the map is
split by :mod:`.watercourse` into ground a farm pond may sit on and ground it may
not: a river and its floodplain, standing water already on the ground, and
drainage lines carrying more than a farm pond's catchment. That split is by
upstream area in hectares, so it does not depend on how much of this particular
map happens to be river.

A cell that is a stream bed but too small to be a nala is dropped as well: a lot
of flow passing through and no hollow deeper than the source could actually
record is interpolation over a channel, not storage.

Nothing is thrown away silently. Nala-class cells are still ranked, separately,
and returned as the structure they really are — a bund or a percolation tank,
with a waste weir — so the answer is "not a pond, this instead" rather than an
empty map. Only the river itself and open water carry no proposal at all.

Sites are then taken best-first while keeping them spaced apart, so the result is
a set of genuine alternatives rather than a cluster of neighbouring cells.

No thresholds in the *scoring* are tied to a particular map: scores are
normalised against the range present in whatever terrain was uploaded. The
thresholds that decide what a place *is* are in :mod:`.watercourse`, in hectares,
and are the same everywhere.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .grid import ElevationGrid
from .watercourse import FARM_POND, NALA, Watercourses

# Score weights; they are normalised, so relative size is what matters.
WEIGHTS = {"depression": 0.30, "catchment": 0.30, "slope": 0.25, "elevation": 0.15}

# Slope at which the slope score has fallen to half. Above this, ground is
# steep enough that excavation and embankment costs climb sharply.
SLOPE_HALF_SCORE_DEG = 8.0

# A cell counts as a bare stream bed when its upstream area is in the top
# CHANNEL_FLOW_PERCENTILE of the map yet holds too little standing depth to be
# storage. This is the small-channel case: gullies and first-order streams below
# the nala threshold, which the hectare rule in `watercourse` lets through.
#
# "Too little" cannot be a fixed number of centimetres. A hollow shallower than
# the source could record is not a shallow hollow, it is interpolation between
# two identical readings, and on a busy channel that is exactly what a smoothing
# filter manufactures. So the bar is the coarser of a floor and the terrain's own
# vertical resolution: the contour interval, or one metre for integer-metre DEM
# tiles. Below that the map simply does not know whether anything is there.
#
# Note what this rule cannot do, and why `watercourse` exists: a *wide* river
# reads as a deep trench, because the sensor sees its water surface and because
# noise, bridges and bank canopy cut into it. Being deep, it is exempted here.
# The size rule catches it first, and must, or this one would wave it through.
CHANNEL_FLOW_PERCENTILE = 95.0
CHANNEL_MIN_DEPRESSION_M = 0.30

# A hollow that one ordinary storm fills and overtops is not storing water, it is
# passing it on, and what has to be built there is a spillway rather than an
# embankment. 50 mm in a day is an ordinary storm across monsoon India, and the
# rational method below is the same one the response already uses for yield, so
# the comparison is like for like rather than a second invented rule.
STORM_TEST_MM = 50.0
STORM_TEST_RUNOFF_C = 0.4

# How many alternative structures to offer on drainage lines too big for a pond.
# Enough to show there is a choice; not so many that they crowd out the answer.
MAX_CHANNEL_STRUCTURES = 3

# How many candidates to examine to fill `max_sites` ponds. Some of what scores
# well turns out, once its storage has actually been worked out, to be a bund
# rather than a pond — and that is only knowable cell by cell, after the flood
# fill. Looking at several times as many leaves room to set those aside without
# the answer coming up short.
CANDIDATE_MULTIPLE = 4

RATINGS = ((70.0, "Excellent"), (55.0, "Good"), (40.0, "Moderate"), (0.0, "Marginal"))

STRUCTURE_LABELS = {
    FARM_POND: "Farm pond",
    NALA: "Nala bund / percolation tank",
}


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
    structure: str = FARM_POND
    upstream_hectares: float = 0.0
    height_above_drainage_m: float | None = None

    @property
    def structure_label(self) -> str:
        return STRUCTURE_LABELS.get(self.structure, self.structure)


@dataclass
class SiteSelection:
    """What the terrain offers, split by the structure each place actually takes."""

    ponds: list[PondSite]
    channel_structures: list[PondSite]


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
        # It cleared the watercourse tests, so the hollow is real and the
        # catchment is farm-pond sized — but the land above it still delivers
        # more in one storm than the hollow can keep.
        reasons.append(
            f"Caution: {upstream_ha:,.0f} ha drains through here, and 50 mm of rain on it "
            f"yields about {storm_m3:,.0f} m³ against the {storage.volume_m3:,.0f} m³ this "
            f"hollow holds. It fills and spills in one ordinary storm, so this is a nala "
            f"bund needing a spillway, not a farm pond"
        )
    return reasons


def _describe_channel(storage: StorageEstimate, upstream_ha: float, storm_m3: float,
                      farm_pond_max_ha: float) -> list[str]:
    """Why this place is a bund rather than a pond, and what that entails.

    Two things send a place here and they deserve different first sentences: a
    catchment larger than a farm pond is sized for, or a hollow that one ordinary
    storm fills and overtops. The second is the same verdict reached by measuring
    rather than by the size rule, and it used to be printed as a caution on a site
    that was still being recommended as a pond.
    """
    if upstream_ha >= farm_pond_max_ha:
        opening = (
            f"{upstream_ha:,.0f} ha drains through here — more than the {farm_pond_max_ha:g} ha a "
            f"farm pond is sized for, so this is a nala bund or percolation tank, not a pond"
        )
    else:
        opening = (
            f"50 mm of rain on the {upstream_ha:,.1f} ha above this point yields about "
            f"{storm_m3:,.0f} m³, against the {storage.volume_m3:,.0f} m³ this hollow holds. It "
            f"fills and spills in one ordinary storm, so it is a bund, not a pond"
        )
    reasons = [
        opening,
        f"50 mm of rain on that catchment yields about {storm_m3:,.0f} m³, so the structure "
        f"has to pass water on, not just hold it: it needs a waste weir or spillway",
    ]
    if storage.volume_m3 > 0:
        reasons.append(
            f"The natural section fills to {storage.spill_elevation_m:g} m, covering "
            f"{storage.surface_area_m2:,.0f} m² and holding about {storage.volume_m3:,.0f} m³ "
            f"before it spills"
        )
    else:
        reasons.append("No natural hollow here — the whole section would have to be impounded")
    reasons.append(
        "Check the downstream and submergence rights before going further; a bund on a "
        "drainage line affects who gets water below it"
    )
    return reasons


def _rating(score: float) -> str:
    return next(label for cutoff, label in RATINGS if score >= cutoff)


def _score_layers(grid: ElevationGrid, accumulation: np.ndarray, depths: np.ndarray):
    """The four normalised suitability layers and their weighted 0-100 sum."""
    components = {
        "depression": _normalise(depths),
        "catchment": _normalise(np.log1p(accumulation.astype(float))),
        "slope": _normalise(1.0 / (1.0 + grid.slope_deg / SLOPE_HALF_SCORE_DEG)),
        "elevation": _normalise(-grid.z),
    }
    total_weight = sum(WEIGHTS.values())
    score = sum(WEIGHTS[name] / total_weight * layer for name, layer in components.items()) * 100.0
    return components, score


def _bare_stream_bed(grid: ElevationGrid, accumulation: np.ndarray, direction: np.ndarray,
                     depths: np.ndarray) -> np.ndarray:
    """Busy channel with no hollow the source could actually have measured."""
    holds_water = max(CHANNEL_MIN_DEPRESSION_M, grid.vertical_resolution_m)
    return (
        (accumulation >= np.percentile(accumulation, CHANNEL_FLOW_PERCENTILE))
        & (depths < holds_water)
        & (direction >= 0)
    )


def _edge_mask(shape: tuple[int, int]) -> np.ndarray:
    """The outer ring, where interpolation is least reliable and flow leaves the map."""
    rows, cols = shape
    edge = max(2, min(rows, cols) // 40)
    mask = np.zeros(shape, dtype=bool)
    mask[:edge, :] = mask[-edge:, :] = True
    mask[:, :edge] = mask[:, -edge:] = True
    return mask


def _order(score: np.ndarray, accumulation: np.ndarray, depths: np.ndarray,
           grid: ElevationGrid) -> list[int]:
    """Every eligible cell, best first.

    Ties break on catchment, then depth, then position, so the same input always
    yields the same output.
    """
    return sorted(
        (int(i) for i in np.flatnonzero(score)),
        key=lambda i: (
            -score.flat[i],
            -accumulation.flat[i],
            -depths.flat[i],
            grid.z.flat[i],
            i,
        ),
    )


def _spaced(cells: list[tuple[int, int]], candidate: tuple[int, int], separation: int) -> bool:
    r, c = candidate
    return all((r - pr) ** 2 + (c - pc) ** 2 >= separation ** 2 for pr, pc in cells)


def _separation(shape: tuple[int, int]) -> int:
    return max(3, min(shape) // 8)


@dataclass
class _Candidate:
    """A cell worth considering, with the measurements that decide what it is."""

    row: int
    col: int
    storage: StorageEstimate
    storm_m3: float
    upstream_ha: float
    height_above_drainage_m: float | None = None

    @property
    def cell(self) -> tuple[int, int]:
        return self.row, self.col

    @property
    def overtopped(self) -> bool:
        """One ordinary storm fills this hollow and spills over it.

        Then what is needed is a spillway, not an embankment. Only meaningful
        where there is a natural hollow to overtop: a site that has to be dug
        gets its capacity from the excavation, which this cannot see, so a bare
        site is left to be judged on its catchment alone.
        """
        return self.storage.volume_m3 > 0 and self.storm_m3 > self.storage.volume_m3


def _evaluate(grid: ElevationGrid, accumulation: np.ndarray, depths: np.ndarray,
              cell: tuple[int, int], above_drainage: np.ndarray) -> _Candidate:
    r, c = cell
    height = float(above_drainage[r, c])
    return _Candidate(
        row=r,
        col=c,
        storage=natural_storage(grid, depths, cell),
        storm_m3=_storm_inflow_m3(int(accumulation[r, c]), grid.cell_area_m2),
        upstream_ha=float(accumulation[r, c]) * grid.cell_area_m2 / 10_000.0,
        # Infinite means the water leaves the map without ever meeting a drainage
        # line, so there is no channel this site stands above. That is unknown,
        # and unknown reported as 0.0 would read as "level with the water" — the
        # exact opposite of what it means.
        height_above_drainage_m=height if np.isfinite(height) else None,
    )


def _pick(grid: ElevationGrid, accumulation: np.ndarray, depths: np.ndarray,
          score: np.ndarray, limit: int, above_drainage: np.ndarray) -> list[_Candidate]:
    """Best-scoring cells, taken in order while keeping them spaced apart."""
    cols = score.shape[1]
    separation = _separation(score.shape)

    chosen: list[_Candidate] = []
    taken: list[tuple[int, int]] = []
    for flat in _order(score, accumulation, depths, grid):
        cell = divmod(flat, cols)
        if not _spaced(taken, cell, separation):
            continue
        taken.append(cell)
        chosen.append(_evaluate(grid, accumulation, depths, cell, above_drainage))
        if len(chosen) >= limit:
            break
    return chosen


def _build(grid: ElevationGrid, accumulation: np.ndarray, depths: np.ndarray,
           components: dict[str, np.ndarray], score: np.ndarray,
           chosen: list[_Candidate], structure: str,
           farm_pond_max_ha: float) -> list[PondSite]:
    sites: list[PondSite] = []
    for rank, candidate in enumerate(chosen, start=1):
        r, c = candidate.cell
        parts = {name: round(float(layer[r, c]), 3) for name, layer in components.items()}
        lat, lon = grid.point_at(r, c)
        site_score = round(float(score[r, c]), 1)
        reasons = (
            _describe(parts, float(depths[r, c]), candidate.storage,
                      candidate.upstream_ha, candidate.storm_m3)
            if structure == FARM_POND
            else _describe_channel(candidate.storage, candidate.upstream_ha,
                                   candidate.storm_m3, farm_pond_max_ha)
        )
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
            storage=candidate.storage,
            reasons=reasons,
            structure=structure,
            upstream_hectares=round(candidate.upstream_ha, 2),
            height_above_drainage_m=(
                None if candidate.height_above_drainage_m is None
                else round(candidate.height_above_drainage_m, 2)
            ),
        ))
    return sites


def rank_sites(
    grid: ElevationGrid,
    accumulation: np.ndarray,
    direction: np.ndarray,
    depths: np.ndarray,
    max_sites: int = 5,
    watercourses: Watercourses | None = None,
) -> list[PondSite]:
    """Score the terrain and return the best-spaced candidate farm-pond sites.

    Cells that are river, standing water, a drainage line larger than a farm
    pond's catchment, or a bare stream bed are removed before anything is chosen,
    so the next-best genuine site is promoted rather than left in the shadow of a
    watercourse it was never competing with.
    """
    return select(grid, accumulation, direction, depths, max_sites, watercourses).ponds


def select(
    grid: ElevationGrid,
    accumulation: np.ndarray,
    direction: np.ndarray,
    depths: np.ndarray,
    max_sites: int = 5,
    watercourses: Watercourses | None = None,
    max_channel_structures: int = MAX_CHANNEL_STRUCTURES,
) -> SiteSelection:
    """Rank pond sites, and separately rank the bunds the big channels would take.

    Two things move a place out of the pond list. The size rule in
    :mod:`.watercourse` handles ground the water has already claimed — river,
    floodplain, standing water, a nala above a farm pond's catchment — and it can
    be applied to the whole map at once. The storm test can only be applied cell
    by cell, because it needs the hollow's actual volume, so candidates are
    picked first and sorted afterwards. Both verdicts land in the same place: the
    site is offered as the structure it really takes.
    """
    from . import watercourse as wc_module  # local: avoids a cycle at import time

    if watercourses is None:
        watercourses = wc_module.classify(grid, accumulation, direction)

    components, score = _score_layers(grid, accumulation, depths)
    edge = _edge_mask(grid.shape)
    bare_bed = _bare_stream_bed(grid, accumulation, direction, depths)

    pond_score = score.copy()
    pond_score[watercourses.excluded | bare_bed | edge] = 0.0
    above = watercourses.height_above_drainage
    examined = _pick(grid, accumulation, depths, pond_score,
                     max_sites * CANDIDATE_MULTIPLE, above)

    ponds = [c for c in examined if not c.overtopped][:max_sites]
    overtopped = [c for c in examined if c.overtopped]

    # The nala class keeps its own ranking: it is a real answer, just not a pond.
    # The river itself and open water get none — nothing responsible can be
    # proposed on either from an elevation model alone.
    channel_score = score.copy()
    channel_score[~watercourses.nala] = 0.0
    channel_score[edge] = 0.0
    channels = _pick(grid, accumulation, depths, channel_score, max_channel_structures, above)

    # A hollow one storm overtops is the same verdict the size rule reaches, just
    # measured rather than assumed, so the two lists are one list. Ordering by
    # score keeps it comparable with everything else on the map.
    separation = _separation(grid.shape)
    taken = [c.cell for c in channels]
    for candidate in sorted(overtopped, key=lambda c: -score[c.cell]):
        if len(channels) >= max_channel_structures:
            break
        if _spaced(taken, candidate.cell, separation):
            taken.append(candidate.cell)
            channels.append(candidate)
    channels.sort(key=lambda c: -score[c.cell])

    return SiteSelection(
        ponds=_build(grid, accumulation, depths, components, pond_score, ponds,
                     FARM_POND, watercourses.farm_pond_max_ha),
        channel_structures=_build(grid, accumulation, depths, components, score, channels,
                                  NALA, watercourses.farm_pond_max_ha),
    )
