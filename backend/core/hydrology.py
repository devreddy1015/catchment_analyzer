"""D8 surface hydrology on an elevation grid.

Everything here follows the standard raster hydrology chain:

    fill sinks -> flow direction -> flow accumulation -> watershed

``flow_direction`` sends each cell to its steepest downhill neighbour of eight
(O'Callaghan & Mark, 1984). ``fill_sinks`` is the priority-flood algorithm
(Wang & Liu, 2006); the depth it adds to a cell is also the natural measure of
how deep a depression sits there, which pond siting uses directly.

A catchment is then the set of cells that drain into a chosen outlet, found by
walking the flow pointers upstream from that outlet. Walking upstream from the
whole drainage network at once instead gives ``height_above_drainage``, which is
how far above the water a piece of ground stands.
"""
from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .geo import haversine_m, path_length_m
from .grid import ElevationGrid

# Neighbour offsets (row, col), clockwise from east. Index into these is what
# `flow_direction` stores; -1 marks a cell with nowhere lower to go.
D8_OFFSETS: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1),
)
_D8_DISTANCE = np.array([1.0 if (dr == 0 or dc == 0) else math.sqrt(2.0) for dr, dc in D8_OFFSETS])

# Filling a pit leaves a dead-flat lake with no downhill neighbour anywhere in it.
# Tilting the fill by this much per cell restores a drainage path across the flat
# without meaningfully changing any elevation (Barnes et al., 2014).
FILL_EPSILON_M = 1e-4


def fill_sinks(z: np.ndarray, epsilon: float = 0.0) -> np.ndarray:
    """Priority-flood depression filling.

    With ``epsilon`` above zero the filled surface is tilted slightly towards the
    outlet, which is what makes flow direction defined inside filled basins.
    """
    rows, cols = z.shape
    filled = z.astype(float, copy=True)
    closed = np.zeros((rows, cols), dtype=bool)

    queue: list[tuple[float, int, int]] = []
    for r in range(rows):
        for c in (0, cols - 1):
            heapq.heappush(queue, (filled[r, c], r, c))
            closed[r, c] = True
    for c in range(cols):
        for r in (0, rows - 1):
            if not closed[r, c]:
                heapq.heappush(queue, (filled[r, c], r, c))
                closed[r, c] = True

    while queue:
        level, r, c = heapq.heappop(queue)
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not closed[nr, nc]:
                closed[nr, nc] = True
                filled[nr, nc] = max(filled[nr, nc], level + epsilon)
                heapq.heappush(queue, (filled[nr, nc], nr, nc))

    return filled


def depression_depth(z: np.ndarray) -> np.ndarray:
    """How much fill each cell needs — i.e. the depth of the hollow it sits in."""
    return np.maximum(0.0, fill_sinks(z) - z)


def conditioned_surface(z: np.ndarray) -> np.ndarray:
    """The surface flow routing should run on: pits filled, flats gently tilted.

    Routing on the raw grid strands water in every hollow and leaves flow
    accumulation meaningless, so this is what feeds ``flow_direction``. Elevation
    and depression depth are still read from the original grid.
    """
    return fill_sinks(z, epsilon=FILL_EPSILON_M)


def flow_direction(z: np.ndarray, cell_size_m: float) -> np.ndarray:
    """Index (0-7) of the steepest downhill neighbour per cell, or -1 for a pit.

    Vectorised: the grid is padded with +inf so off-grid neighbours can never
    win, then all eight candidate drops are compared at once.
    """
    rows, cols = z.shape
    padded = np.pad(z.astype(float), 1, constant_values=np.inf)

    best_slope = np.zeros((rows, cols))
    direction = np.full((rows, cols), -1, dtype=np.int8)

    for index, (dr, dc) in enumerate(D8_OFFSETS):
        neighbour = padded[1 + dr: 1 + dr + rows, 1 + dc: 1 + dc + cols]
        slope = (z - neighbour) / (cell_size_m * _D8_DISTANCE[index])
        steeper = slope > best_slope
        best_slope = np.where(steeper, slope, best_slope)
        direction = np.where(steeper, index, direction)

    return direction


def _receiver_index(direction: np.ndarray) -> np.ndarray:
    """Flat index of each cell's downstream neighbour, or -1 where there is none."""
    rows, cols = direction.shape
    rr, cc = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")

    receiver = np.full(direction.shape, -1, dtype=np.int64)
    for index, (dr, dc) in enumerate(D8_OFFSETS):
        move = direction == index
        if not move.any():
            continue
        nr, nc = rr + dr, cc + dc
        inside = move & (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
        receiver[inside] = (nr * cols + nc)[inside]

    return receiver


def flow_accumulation(z: np.ndarray, direction: np.ndarray) -> np.ndarray:
    """Number of cells draining through each cell, itself included.

    Water only ever moves downhill, so visiting cells from the highest down
    guarantees a cell's own total is final before it is passed downstream.
    """
    receiver = _receiver_index(direction).ravel()
    accumulation = np.ones(z.size, dtype=np.int64)

    for flat in np.argsort(z, axis=None)[::-1]:
        downstream = receiver[flat]
        if downstream >= 0:
            accumulation[downstream] += accumulation[flat]

    return accumulation.reshape(z.shape)


def upstream_cells(direction: np.ndarray, outlet: tuple[int, int]) -> set[tuple[int, int]]:
    """Every cell that drains to ``outlet``, found by walking flow pointers upstream."""
    rows, cols = direction.shape
    contributors = _contributors(direction)

    start = outlet[0] * cols + outlet[1]
    catchment = {start}
    queue = deque([start])
    while queue:
        for source in contributors.get(queue.popleft(), ()):
            if source not in catchment:
                catchment.add(source)
                queue.append(source)

    return {divmod(flat, cols) for flat in catchment}


def _contributors(direction: np.ndarray) -> dict[int, list[int]]:
    """For each cell, the flat indices of the cells that drain directly into it."""
    contributors: dict[int, list[int]] = {}
    for flat, downstream in enumerate(_receiver_index(direction).ravel()):
        if downstream >= 0:
            contributors.setdefault(int(downstream), []).append(flat)
    return contributors


def trunk_upstream(accumulation: np.ndarray, direction: np.ndarray,
                   start: tuple[int, int]) -> list[tuple[int, int]]:
    """Walk up the main channel from a cell, always taking the largest feeder.

    At every junction the bigger of the two branches is the same watercourse
    continuing and the smaller is a tributary joining it, so following the
    largest contributor traces one river rather than wandering into its
    headwaters. The walk ends where the water does: on a hilltop, or at the edge
    of the map, and which of the two it is matters a great deal.
    """
    cols = direction.shape[1]
    contributors = _contributors(direction)
    flat_accumulation = accumulation.ravel()

    flat = start[0] * cols + start[1]
    path = [flat]
    seen = {flat}
    while True:
        feeders = contributors.get(flat)
        if not feeders:
            break
        flat = max(feeders, key=lambda i: flat_accumulation[i])
        if flat in seen:
            break
        seen.add(flat)
        path.append(flat)

    return [divmod(f, cols) for f in path]


def height_above_drainage(z: np.ndarray, direction: np.ndarray, network: np.ndarray) -> np.ndarray:
    """How far each cell stands above the drainage line its own water reaches.

    HAND, the height above nearest drainage (Rennó et al. 2008; Nobre et al.
    2011). Following the flow pointers downstream from a cell, the first cell of
    ``network`` they meet is the channel that cell belongs to, and the elevation
    difference between the two is the answer.

    Distance to the nearest channel says almost nothing about whether water
    reaches a place — ground fifty metres from a river but eight metres above it
    is dry, and ground three hundred metres away but level with it floods. Height
    above the channel a cell actually drains into is the thing that separates
    them, which is why this is the standard index for floodplain extent.

    Walking upstream from the network reaches every cell exactly once and needs
    no elevation ordering: the first network cell downstream of a cell is, by
    construction, the one its receiver already found. Cells whose water leaves
    the map without meeting the network come back as ``inf`` — unknown, which is
    not the same as zero.
    """
    rows, cols = z.shape
    reference = np.full(z.size, -1, dtype=np.int64)

    seeds = np.flatnonzero(network.ravel())
    reference[seeds] = seeds

    contributors = _contributors(direction)
    queue = deque(int(seed) for seed in seeds)
    while queue:
        downstream = queue.popleft()
        for source in contributors.get(downstream, ()):
            if reference[source] < 0:
                reference[source] = reference[downstream]
                queue.append(source)

    flat_z = z.ravel()
    height = np.full(z.size, np.inf)
    reached = reference >= 0
    height[reached] = flat_z[reached] - flat_z[reference[reached]]
    return np.maximum(height, 0.0).reshape(rows, cols)


def longest_flow_path(grid: ElevationGrid, direction: np.ndarray, cells: set[tuple[int, int]]) -> list[list[float]]:
    """The longest single drainage path inside a catchment, as (lon, lat) points.

    Its length drives the time of concentration, which is what sizes a pond's
    spillway, so it is worth reporting alongside the area.
    """
    rows, cols = direction.shape
    sources = cells - {
        (r + dr, c + dc)
        for r, c in cells
        if direction[r, c] >= 0
        for dr, dc in (D8_OFFSETS[direction[r, c]],)
    }

    best: list[list[float]] = []
    best_length = -1.0
    for start in sources:
        path: list[list[float]] = []
        r, c = start
        seen: set[tuple[int, int]] = set()
        while (r, c) in cells and (r, c) not in seen and 0 <= r < rows and 0 <= c < cols:
            seen.add((r, c))
            lat, lon = grid.point_at(r, c)
            path.append([round(lon, 6), round(lat, 6)])
            step = direction[r, c]
            if step < 0:
                break
            dr, dc = D8_OFFSETS[step]
            r, c = r + dr, c + dc

        length = path_length_m(path)
        if length > best_length:
            best_length, best = length, path

    return best


def _cells_to_polygon(grid: ElevationGrid, cells: set[tuple[int, int]]) -> list[list[float]]:
    """Outline of a cell set as a single (lon, lat) ring."""
    if not cells:
        return []

    rows, cols = grid.shape
    half_lat = (grid.north - grid.south) / max(1, rows - 1) / 2.0
    half_lon = (grid.east - grid.west) / max(1, cols - 1) / 2.0

    squares = []
    for r, c in cells:
        lat, lon = grid.point_at(r, c)
        squares.append(Polygon([
            (lon - half_lon, lat - half_lat),
            (lon + half_lon, lat - half_lat),
            (lon + half_lon, lat + half_lat),
            (lon - half_lon, lat + half_lat),
        ]))

    merged = unary_union(squares)
    if merged.geom_type == "MultiPolygon":
        merged = max(merged.geoms, key=lambda part: part.area)

    outline = merged.simplify(min(half_lat, half_lon) / 2.0, preserve_topology=True)
    return [[round(x, 6), round(y, 6)] for x, y in outline.exterior.coords]


@dataclass
class Catchment:
    """The drainage area contributing to one outlet."""

    outlet: tuple[float, float]          # (lat, lon)
    cells: set[tuple[int, int]]
    boundary: list[list[float]]          # closed (lon, lat) ring
    area_m2: float
    perimeter_m: float
    mean_slope_deg: float
    max_slope_deg: float
    relief_m: float
    min_elevation_m: float
    max_elevation_m: float
    flow_path: list[list[float]]
    flow_path_length_m: float

    @property
    def area_km2(self) -> float:
        return self.area_m2 / 1_000_000.0

    @property
    def average_gradient(self) -> float:
        """Relief over flow-path length — the catchment's overall steepness."""
        return self.relief_m / self.flow_path_length_m if self.flow_path_length_m else 0.0


def delineate(grid: ElevationGrid, direction: np.ndarray, outlet: tuple[int, int]) -> Catchment:
    """Build the full catchment description for an outlet cell."""
    cells = upstream_cells(direction, outlet)
    rows = np.fromiter((r for r, _ in cells), dtype=int, count=len(cells))
    cols = np.fromiter((c for _, c in cells), dtype=int, count=len(cells))

    elevations = grid.z[rows, cols]
    slopes = grid.slope_deg[rows, cols]

    boundary = _cells_to_polygon(grid, cells)
    perimeter_m = sum(
        haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(boundary, boundary[1:])
    )

    flow_path = longest_flow_path(grid, direction, cells)
    outlet_lat, outlet_lon = grid.point_at(*outlet)

    return Catchment(
        outlet=(outlet_lat, outlet_lon),
        cells=cells,
        boundary=boundary,
        area_m2=len(cells) * grid.cell_area_m2,
        perimeter_m=perimeter_m,
        mean_slope_deg=float(slopes.mean()),
        max_slope_deg=float(slopes.max()),
        relief_m=float(elevations.max() - elevations.min()),
        min_elevation_m=float(elevations.min()),
        max_elevation_m=float(elevations.max()),
        flow_path=flow_path,
        flow_path_length_m=path_length_m(flow_path),
    )


def trace_lines(grid: ElevationGrid, direction: np.ndarray, mask: np.ndarray) -> list[list[list[float]]]:
    """Follow the flow pointers through a set of cells, as (lon, lat) polylines.

    Any boolean selection of cells that lies along the drainage network — the
    stream network, or the reach classed as river — comes out as lines a map can
    draw, rather than as a scatter of squares.
    """
    rows, cols = grid.shape
    visited = np.zeros_like(mask)
    segments: list[list[list[float]]] = []

    for r, c in zip(*np.nonzero(mask)):
        if visited[r, c]:
            continue
        points: list[list[float]] = []
        while 0 <= r < rows and 0 <= c < cols and mask[r, c]:
            lat, lon = grid.point_at(r, c)
            points.append([round(lon, 6), round(lat, 6)])
            if visited[r, c]:
                break
            visited[r, c] = True
            step = direction[r, c]
            if step < 0:
                break
            dr, dc = D8_OFFSETS[step]
            r, c = r + dr, c + dc
        if len(points) >= 2:
            segments.append(points)

    return segments


def stream_network(grid: ElevationGrid, direction: np.ndarray, accumulation: np.ndarray,
                   threshold_fraction: float = 0.01) -> list[list[list[float]]]:
    """Drainage lines: cells whose upstream area exceeds a share of the map,
    traced downstream into polylines for display."""
    rows, cols = grid.shape
    threshold = max(5, int(threshold_fraction * rows * cols))
    return trace_lines(grid, direction, accumulation >= threshold)
