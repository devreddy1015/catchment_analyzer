"""Turn contour lines into a regular elevation grid (a small DEM).

Contours say where a given elevation runs; hydrology needs a value in every
cell. The conversion is:

1. every contour vertex becomes a control point ``(lon, lat, elevation)``,
   sub-sampled evenly so a densely digitised contour does not outvote a sparse one;
2. a Delaunay triangulation over those points gives linear interpolation inside
   the surveyed area — the classic TIN approach, and it reproduces the original
   elevations exactly along the contours themselves;
3. cells outside the convex hull of the survey fall back to nearest-neighbour,
   which is flat but stable — the fraction of such cells is reported so the
   caller knows how much of the surface is extrapolated;
4. one light 3x3 average removes the faceting seams left by the triangulation.

Grid shape follows the ground: rows and columns are chosen so that a cell is
close to square in metres, which is what the D8 flow model assumes.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from scipy.ndimage import uniform_filter

from .geo import haversine_m
from .kml import ContourSet

MAX_CONTROL_POINTS = 20_000
MIN_SIDE_CELLS = 12

# One light average over the interpolated surface. On a contour grid it removes
# triangulation seams; on a downloaded DEM it removes the terracing left by
# elevations quantised to whole metres, which would otherwise read as flats.
SMOOTH_WINDOW = 3


@dataclass
class ElevationGrid:
    """A north-up raster of elevations with its geographic footprint."""

    z: np.ndarray            # (rows, cols); row 0 is the northern edge
    south: float
    west: float
    north: float
    east: float
    cell_size_m: float
    extrapolated_fraction: float = 0.0

    # The smallest elevation difference the source could actually record: the
    # contour interval of a survey, or the quantisation of a downloaded DEM.
    # Anything shallower than this is interpolation, not measurement, and the
    # siting rules need to know the difference.
    vertical_resolution_m: float = 0.0

    @property
    def shape(self) -> tuple[int, int]:
        return self.z.shape

    @property
    def cell_area_m2(self) -> float:
        return self.cell_size_m ** 2

    @cached_property
    def lats(self) -> np.ndarray:
        return np.linspace(self.north, self.south, self.z.shape[0])

    @cached_property
    def lons(self) -> np.ndarray:
        return np.linspace(self.west, self.east, self.z.shape[1])

    @cached_property
    def slope_deg(self) -> np.ndarray:
        """Per-cell slope from the elevation gradient, in degrees."""
        dz_dy, dz_dx = np.gradient(self.z, self.cell_size_m)
        return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))

    def cell_at(self, lat: float, lon: float) -> tuple[int, int]:
        """Nearest grid cell to a geographic point, clamped to the grid."""
        rows, cols = self.z.shape
        row = round((self.north - lat) / max(self.north - self.south, 1e-12) * (rows - 1))
        col = round((lon - self.west) / max(self.east - self.west, 1e-12) * (cols - 1))
        return int(np.clip(row, 0, rows - 1)), int(np.clip(col, 0, cols - 1))

    def point_at(self, row: int, col: int) -> tuple[float, float]:
        """Geographic centre (lat, lon) of a grid cell."""
        return float(self.lats[row]), float(self.lons[col])

    def bounds_dict(self) -> dict[str, float]:
        return {
            "south": round(self.south, 7),
            "west": round(self.west, 7),
            "north": round(self.north, 7),
            "east": round(self.east, 7),
        }


def _control_points(contours: ContourSet) -> tuple[np.ndarray, np.ndarray]:
    """Sample (lon, lat) control points and their elevations from the contours."""
    total = contours.vertex_count
    step = max(1, total // MAX_CONTROL_POINTS)

    xy: list[tuple[float, float]] = []
    z: list[float] = []
    for line in contours.lines:
        points = line.coordinates[::step] if step > 1 else line.coordinates
        if points[-1] != line.coordinates[-1]:
            points = points + [line.coordinates[-1]]
        xy.extend(points)
        z.extend([line.elevation_m] * len(points))

    return np.asarray(xy, dtype=float), np.asarray(z, dtype=float)


def grid_shape(south: float, west: float, north: float, east: float, resolution: int) -> tuple[int, int, float]:
    """Rows, cols and cell size that keep grid cells close to square on the ground.

    ``resolution`` is the number of cells along the longer side; the shorter side
    gets however many keep a cell square in metres, which is what the D8 flow
    model assumes. Shared by every terrain source, so a grid built from a
    downloaded DEM has exactly the geometry one built from contours would.
    """
    mid_lat = (north + south) / 2.0
    width_m = haversine_m(mid_lat, west, mid_lat, east)
    height_m = haversine_m(south, west, north, west)

    if width_m >= height_m:
        cols = resolution
        rows = max(MIN_SIDE_CELLS, round(resolution * height_m / max(width_m, 1e-9)))
    else:
        rows = resolution
        cols = max(MIN_SIDE_CELLS, round(resolution * width_m / max(height_m, 1e-9)))

    cell_size_m = (width_m / cols + height_m / rows) / 2.0
    return int(rows), int(cols), max(0.5, cell_size_m)


def build_grid(contours: ContourSet, resolution: int = 160, margin: float = 0.01) -> ElevationGrid:
    """Interpolate a contour set onto a regular elevation grid.

    ``resolution`` is the number of cells along the longer side of the map;
    ``margin`` pads the extent so edge contours are not clipped.
    """
    south, west, north, east = contours.bounds
    pad_lat = (north - south) * margin
    pad_lon = (east - west) * margin
    south, north = south - pad_lat, north + pad_lat
    west, east = west - pad_lon, east + pad_lon

    rows, cols, cell_size_m = grid_shape(south, west, north, east, resolution)

    xy, z = _control_points(contours)
    if len(xy) < 4:
        raise ValueError("Not enough contour vertices to interpolate a surface.")

    mesh_lon, mesh_lat = np.meshgrid(
        np.linspace(west, east, cols),
        np.linspace(north, south, rows),  # north-up
    )
    targets = np.column_stack([mesh_lon.ravel(), mesh_lat.ravel()])

    values = LinearNDInterpolator(xy, z)(targets)

    outside = np.isnan(values)
    if outside.any():
        values[outside] = NearestNDInterpolator(xy, z)(targets[outside])

    surface = uniform_filter(values.reshape(rows, cols), size=SMOOTH_WINDOW, mode="nearest")
    if not np.isfinite(surface).all():
        raise ValueError("Interpolation produced an incomplete surface.")

    return ElevationGrid(
        z=np.round(surface, 3),
        south=south,
        west=west,
        north=north,
        east=east,
        cell_size_m=round(cell_size_m, 3),
        extrapolated_fraction=round(float(outside.mean()), 4),
        vertical_resolution_m=contours.interval_m,
    )
