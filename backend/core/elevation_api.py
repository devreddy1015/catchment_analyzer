"""Terrain read straight from the OpenZenith global elevation service.

The rest of the pipeline only ever needs one thing from a terrain source: an
elevation for every point of a regular lat/lon grid. A contour file provides it
by interpolation (``core.grid``); this module provides it by download, so an
area can be analysed anywhere on Earth with no survey to upload.

    https://openzenith.org   —   free, open, no key and no account

Two endpoints are used, both plain GETs:

``GET /api/tile/{z}/{x}/{y}``
    A 256x256 raster of int16 metres for one web-mercator tile, row-major from
    the top-left corner, ``-32768`` for no data. This is the bulk sampler: a
    single request carries 65,536 elevations, so a whole study area is a handful
    of requests rather than tens of thousands.

``GET /api/elevation?lat=&lon=``
    One point, as JSON. Used to check a location before committing to a full
    download, and to report what the service says about the source dataset.

``GET /api/geocode?query=``
    Place names to coordinates, proxied from Nominatim. Geocoding is not terrain,
    but it is here for the same reason the point lookup is: an area analysis needs
    a latitude and longitude, and nobody knows the coordinates of their own
    village. It turns "durg" into 21.1896 N, 81.2851 E and stops there.

The service also documents ``POST /api/elevation/batch``, but that route sits
behind an interactive bot check and cannot be called from a server, so the tile
raster is what this client uses.

Downloaded tiles are immutable and are cached on disk, which makes re-running an
analysis over the same ground essentially free.
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
from scipy.ndimage import uniform_filter

from ..config import settings
from .grid import SMOOTH_WINDOW, ElevationGrid, grid_shape

TILE_PX = 256
TILE_BYTES = TILE_PX * TILE_PX * 2
NODATA = -32768

# Tiles are int16 metres, so a whole metre is the smallest difference the service
# can express. Any hollow shallower than that came out of interpolation, not out
# of the ground.
TILE_VERTICAL_STEP_M = 1.0

# Ground size of one pixel at zoom 0, at the equator (web mercator).
EQUATOR_M = 40_075_016.686
BASE_PIXEL_M = EQUATOR_M / TILE_PX

# Web mercator has no data beyond these latitudes, and neither does the service.
MAX_MERCATOR_LAT = 85.0511


class ElevationServiceError(RuntimeError):
    """The elevation service could not supply terrain for this area."""


@dataclass(frozen=True)
class DemSource:
    """What the service was asked for, and what came back."""

    provider: str
    dataset: str
    zoom: int
    tiles: int
    sample_spacing_m: float
    nodata_fraction: float


# --------------------------------------------------------------------------- #
# Web mercator                                                                 #
# --------------------------------------------------------------------------- #

def pixel_of(lon: float | np.ndarray, lat: float | np.ndarray, zoom: int):
    """Global pixel coordinates of a point at ``zoom``, origin at the top left."""
    span = TILE_PX * (2 ** zoom)
    sin_lat = np.sin(np.radians(np.clip(lat, -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT)))
    x = (np.asarray(lon, dtype=float) + 180.0) / 360.0 * span
    y = (0.5 - np.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * span
    return x, y


def pixel_size_m(lat: float, zoom: int) -> float:
    """Ground length of one tile pixel at this latitude, in metres."""
    return BASE_PIXEL_M * math.cos(math.radians(lat)) / (2 ** zoom)


def zoom_for(cell_size_m: float, lat: float) -> int:
    """The coarsest zoom whose pixels are still finer than one analysis cell.

    Sampling a tile more finely than the grid it feeds only wastes requests, and
    sampling it more coarsely throws away relief the analysis needs.
    """
    for zoom in range(1, settings.ELEVATION_MAX_ZOOM + 1):
        if pixel_size_m(lat, zoom) <= max(cell_size_m, 1.0):
            return zoom
    return settings.ELEVATION_MAX_ZOOM


def tile_span(south: float, west: float, north: float, east: float,
              zoom: int) -> tuple[int, int, int, int]:
    """Range of tiles ``(x0, y0, x1, y1)`` covering a bounding box, inclusive."""
    left, top = pixel_of(west, north, zoom)
    right, bottom = pixel_of(east, south, zoom)
    limit = 2 ** zoom - 1
    x0, x1 = int(left // TILE_PX), int(right // TILE_PX)
    y0, y1 = int(top // TILE_PX), int(bottom // TILE_PX)
    return (max(0, x0), max(0, y0), min(limit, x1), min(limit, y1))


# --------------------------------------------------------------------------- #
# Tile fetching                                                                #
# --------------------------------------------------------------------------- #

def _cache_path(zoom: int, x: int, y: int) -> Path:
    return settings.ELEVATION_CACHE_DIR / f"{zoom}_{x}_{y}.i16"


def _cached(zoom: int, x: int, y: int) -> np.ndarray | None:
    path = _cache_path(zoom, x, y)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return _decode(raw) if len(raw) == TILE_BYTES else None


def _store(zoom: int, x: int, y: int, raw: bytes) -> None:
    try:
        settings.ELEVATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(zoom, x, y).write_bytes(raw)
    except OSError:
        pass  # A cache that cannot be written is not a reason to fail an analysis.


def _decode(raw: bytes) -> np.ndarray:
    """One tile's bytes as a float array of metres, no-data as NaN."""
    tile = np.frombuffer(raw, dtype="<i2").reshape(TILE_PX, TILE_PX).astype(np.float32)
    return np.where(tile <= NODATA + 1, np.nan, tile)


def prune_cache(keep: int | None = None) -> None:
    """Drop the least recently downloaded tiles so the cache stays bounded."""
    directory = settings.ELEVATION_CACHE_DIR
    if not directory.exists():
        return
    limit = settings.ELEVATION_CACHE_TILES if keep is None else keep
    tiles = sorted(directory.glob("*.i16"), key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in tiles[limit:]:
        stale.unlink(missing_ok=True)


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.ELEVATION_API_URL,
        timeout=settings.ELEVATION_TIMEOUT_S,
        headers={"User-Agent": settings.user_agent, "Accept": "*/*"},
        follow_redirects=True,
    )


def _get(client: httpx.Client, url: str, check=None, **kwargs) -> httpx.Response:
    """GET with retries, because a free edge service drops the odd connection.

    ``check`` may raise :class:`ElevationServiceError` to reject a response that
    arrived but is unusable; doing that inside the loop means a truncated body is
    retried like a dropped one, rather than being treated as an answer.
    """
    last: Exception | None = None
    for attempt in range(max(1, settings.ELEVATION_RETRIES)):
        try:
            response = client.get(url, **kwargs)
            response.raise_for_status()
            if check is not None:
                check(response)
            return response
        except (httpx.HTTPError, ElevationServiceError) as exc:
            last = exc
            if attempt < settings.ELEVATION_RETRIES - 1:
                time.sleep(0.4 * (2 ** attempt))

    raise ElevationServiceError(f"{url}: {last}")


def fetch_tile(client: httpx.Client, zoom: int, x: int, y: int) -> np.ndarray:
    """One elevation tile, from the disk cache or the service."""
    hit = _cached(zoom, x, y)
    if hit is not None:
        return hit

    def whole_tile(response: httpx.Response) -> None:
        if len(response.content) != TILE_BYTES:
            raise ElevationServiceError(
                f"expected {TILE_BYTES} bytes, got {len(response.content)}"
            )

    try:
        raw = _get(client, f"/api/tile/{zoom}/{x}/{y}", check=whole_tile).content
    except ElevationServiceError as exc:
        raise ElevationServiceError(f"Could not fetch elevation tile {zoom}/{x}/{y}: {exc}") from exc

    _store(zoom, x, y, raw)
    return _decode(raw)


def _mosaic(zoom: int, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """Every tile covering the area, stitched into one raster."""
    wanted = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    canvas = np.full(((y1 - y0 + 1) * TILE_PX, (x1 - x0 + 1) * TILE_PX), np.nan, np.float32)

    with _client() as client:
        workers = max(1, min(settings.ELEVATION_WORKERS, len(wanted)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            tiles = pool.map(lambda t: fetch_tile(client, zoom, *t), wanted)
            for (x, y), tile in zip(wanted, tiles):
                row, col = (y - y0) * TILE_PX, (x - x0) * TILE_PX
                canvas[row:row + TILE_PX, col:col + TILE_PX] = tile

    prune_cache()
    return canvas


def _bilinear(mosaic: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Sample a raster at fractional pixel positions, bilinearly.

    ``xs``/``ys`` are continuous pixel coordinates. A stored pixel is the reading
    at the *centre* of the square it covers, so index ``i`` sits at coordinate
    ``i + 0.5``; without that half-pixel shift every elevation would be read half
    a pixel up and to the left of where it belongs.
    """
    height, width = mosaic.shape
    grid_x, grid_y = np.meshgrid(xs - 0.5, ys - 0.5)

    x0 = np.clip(np.floor(grid_x), 0, width - 2).astype(int)
    y0 = np.clip(np.floor(grid_y), 0, height - 2).astype(int)
    fx = np.clip(grid_x - x0, 0.0, 1.0)
    fy = np.clip(grid_y - y0, 0.0, 1.0)

    return (
        mosaic[y0, x0] * (1 - fx) * (1 - fy)
        + mosaic[y0, x0 + 1] * fx * (1 - fy)
        + mosaic[y0 + 1, x0] * (1 - fx) * fy
        + mosaic[y0 + 1, x0 + 1] * fx * fy
    )


# --------------------------------------------------------------------------- #
# What the pipeline calls                                                      #
# --------------------------------------------------------------------------- #

def sample_area(south: float, west: float, north: float, east: float,
                rows: int, cols: int, cell_size_m: float) -> tuple[np.ndarray, DemSource]:
    """Elevations for a ``rows x cols`` north-up grid over a bounding box.

    Returns the surface in metres together with a description of where it came
    from. Raises :class:`ElevationServiceError` if the area cannot be covered.
    """
    if not settings.ELEVATION_API_ENABLED:
        raise ElevationServiceError("The elevation service is disabled on this server.")

    mid_lat = (north + south) / 2.0
    zoom = zoom_for(cell_size_m, mid_lat)

    # Back off a zoom at a time until the download is a sane number of requests.
    while zoom > 1:
        x0, y0, x1, y1 = tile_span(south, west, north, east, zoom)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= settings.ELEVATION_MAX_TILES:
            break
        zoom -= 1

    x0, y0, x1, y1 = tile_span(south, west, north, east, zoom)
    tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    if tiles > settings.ELEVATION_MAX_TILES:
        raise ElevationServiceError(
            f"That area needs {tiles} terrain tiles; the limit is {settings.ELEVATION_MAX_TILES}. "
            "Ask for a smaller area."
        )

    mosaic = _mosaic(zoom, x0, y0, x1, y1)

    xs, _ = pixel_of(np.linspace(west, east, cols), np.full(cols, mid_lat), zoom)
    _, ys = pixel_of(np.full(rows, (west + east) / 2.0), np.linspace(north, south, rows), zoom)
    surface = _bilinear(mosaic, xs - x0 * TILE_PX, ys - y0 * TILE_PX)

    missing = ~np.isfinite(surface)
    if missing.all():
        raise ElevationServiceError(
            "The elevation service has no terrain data for this area — it may be open ocean "
            "or outside the coverage of the source dataset."
        )
    if missing.any():
        # Isolated gaps: fill from the mean rather than abandoning the analysis.
        surface = np.where(missing, float(np.nanmean(surface)), surface)

    source = DemSource(
        provider="OpenZenith",
        dataset="Copernicus GLO-30 / GEBCO 2025",
        zoom=zoom,
        tiles=tiles,
        sample_spacing_m=round(pixel_size_m(mid_lat, zoom), 2),
        nodata_fraction=round(float(missing.mean()), 4),
    )
    return surface.astype(float), source


def search_places(query: str, limit: int = 5) -> list[dict]:
    """Places matching a name, best match first.

    Returns an empty list when nothing matches, which is an answer rather than a
    failure; only an unreachable service raises.
    """
    if not settings.ELEVATION_API_ENABLED:
        raise ElevationServiceError("The elevation service is disabled on this server.")
    try:
        with _client() as client:
            body = _get(client, "/api/geocode", params={"query": query, "limit": limit}).json()
    except (ElevationServiceError, ValueError) as exc:
        raise ElevationServiceError(f"Place search failed: {exc}") from exc

    places = []
    for found in body.get("results", []):
        try:
            places.append({
                "name": str(found["display_name"]),
                "latitude": float(found["lat"]),
                "longitude": float(found["lon"]),
                "kind": found.get("type"),
            })
        except (KeyError, TypeError, ValueError):
            continue  # A result missing coordinates is one we cannot put on a map.
    return places


def point_elevation(lat: float, lon: float) -> dict:
    """Elevation at a single point, as the service reports it.

    Cheap enough to call before an analysis, which is what makes it useful: it
    says whether a location is land, and how good the data there is, for the
    price of one request.
    """
    if not settings.ELEVATION_API_ENABLED:
        raise ElevationServiceError("The elevation service is disabled on this server.")
    try:
        with _client() as client:
            body = _get(client, "/api/elevation", params={"lat": lat, "lon": lon}).json()
    except (ElevationServiceError, ValueError) as exc:
        raise ElevationServiceError(f"Elevation lookup failed: {exc}") from exc

    if body.get("elevation") is None:
        raise ElevationServiceError(f"No elevation data at {lat:.5f}, {lon:.5f}.")

    return {
        "latitude": lat,
        "longitude": lon,
        "elevation_m": float(body["elevation"]),
        "surface_type": body.get("surface_type", "unknown"),
        "resolution_m": body.get("resolution"),
        "source": body.get("source", "openzenith"),
    }


def fetch_grid(south: float, west: float, north: float, east: float,
               resolution: int) -> tuple[ElevationGrid, DemSource]:
    """Download a bounding box as an elevation grid the analysis can run on.

    The grid geometry is the same one a contour file would produce for the same
    extent, so everything downstream — flow routing, siting, the overlays —
    behaves identically whichever way the terrain arrived.
    """
    if north <= south or east <= west:
        raise ElevationServiceError("The requested area has no extent.")

    # One point first. The service merges ocean bathymetry with land elevation, so
    # the sea has a perfectly good surface with hollows in it, and an analysis of
    # it would return a confident pond site on the abyssal plain. Asking costs one
    # round trip and refuses in a sentence instead of a seabed.
    #
    # Anything the service does not call "land" is water: it labels the deep ocean
    # "seafloor" where its own schema says "ocean", so the test has to be for what
    # is wanted rather than for a list of what is not. Land genuinely below sea
    # level — the Dead Sea shore, a polder — comes back as "land" and is kept.
    centre = point_elevation((south + north) / 2.0, (west + east) / 2.0)
    if centre["surface_type"] != "land":
        raise ElevationServiceError(
            f"The middle of that area is water — the service calls it "
            f"'{centre['surface_type']}' at {centre['elevation_m']:.0f} m. "
            f"Pick somewhere on land."
        )

    rows, cols, cell_size_m = grid_shape(south, west, north, east, resolution)
    surface, source = sample_area(south, west, north, east, rows, cols, cell_size_m)
    surface = uniform_filter(surface, size=SMOOTH_WINDOW, mode="nearest")

    grid = ElevationGrid(
        z=np.round(surface, 3),
        south=south,
        west=west,
        north=north,
        east=east,
        cell_size_m=round(cell_size_m, 3),
        extrapolated_fraction=source.nodata_fraction,
        vertical_resolution_m=TILE_VERTICAL_STEP_M,
    )
    if float(grid.z.max() - grid.z.min()) < 0.5:
        raise ElevationServiceError(
            "The terrain here is flat to within half a metre, so there is nothing for "
            "water to run down. Try a larger area or somewhere with more relief."
        )
    return grid, source
