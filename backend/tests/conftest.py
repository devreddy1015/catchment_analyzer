"""Shared fixtures: a real client, the sample map, a synthetic map, and a
stand-in for the elevation service.

The synthetic map is the important one for uploads — it proves the analysis is
driven by the uploaded file rather than tuned to the sample, because it sits
somewhere else on Earth, at a different scale, with a different contour interval.

:class:`FakeService` does the same job for downloaded terrain: it answers tile
requests with a hill computed from each pixel's real latitude and longitude, so
tests can check the projection arithmetic against a surface they already know,
and no test ever needs the network.
"""
from __future__ import annotations

import math
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.core import elevation_api
from backend.core.elevation_api import TILE_PX
from backend.main import app

SAMPLE_KML = Path(__file__).resolve().parents[2] / "contours_1m.kml"


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="session")
def sample_kml() -> bytes:
    if not SAMPLE_KML.exists():
        pytest.skip(f"Sample contour map not found at {SAMPLE_KML}")
    return SAMPLE_KML.read_bytes()


def make_kml(centre_lat: float, centre_lon: float, span_deg: float,
             levels: range, interval_m: float) -> bytes:
    """A conical hill as concentric contour rings, anywhere on Earth.

    Radius shrinks as elevation rises, so the surface is a cone with a single
    summit and a well-defined drainage pattern.
    """
    placemarks = []
    top = max(levels)
    for level in levels:
        elevation = level * interval_m
        radius = span_deg * (1.0 - 0.85 * level / top) / 2.0
        ring = " ".join(
            f"{centre_lon + radius * math.cos(t):.7f},{centre_lat + radius * math.sin(t) * 0.6:.7f}"
            for t in (2 * math.pi * i / 48 for i in range(49))
        )
        placemarks.append(
            f"<Placemark><name>{elevation}</name>"
            f"<LineString><coordinates>{ring}</coordinates></LineString></Placemark>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f'{"".join(placemarks)}'
        "</Document></kml>"
    ).encode()


@pytest.fixture(scope="session")
def synthetic_kml() -> bytes:
    """A 5 m-interval hill in Kenya — nothing like the sample map."""
    return make_kml(centre_lat=-1.2921, centre_lon=36.8219, span_deg=0.02,
                    levels=range(1, 21), interval_m=5.0)


@pytest.fixture(scope="session")
def synthetic_kmz(synthetic_kml: bytes) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.kml", synthetic_kml)
    return buffer.getvalue()


# The synthetic hill: a cone in the Chhattisgarh plains, big enough to drain.
HILL_LAT, HILL_LON = 21.2500, 81.3000
HILL_BASE_M, HILL_HEIGHT_M, HILL_RADIUS_DEG = 250.0, 60.0, 0.030


# What the fake geocoder knows about, including one entry with no usable
# coordinates — Nominatim results are third-party data and are not all well formed.
PLACES = [
    {"display_name": "Durg, Chhattisgarh, India", "lat": 21.1983, "lon": 81.4008, "type": "administrative"},
    {"display_name": "Durg, Durg Tahsil, Chhattisgarh, 491002, India", "lat": 21.1896, "lon": 81.2851, "type": "city"},
    {"display_name": "Durgapur, West Bengal, India", "lat": None, "lon": None, "type": "city"},
]


def hill_elevation(lat, lon):
    """The analytic surface the fake service serves, in metres."""
    distance = np.hypot(np.asarray(lat) - HILL_LAT, (np.asarray(lon) - HILL_LON) * 0.93)
    return HILL_BASE_M + HILL_HEIGHT_M * np.clip(1.0 - distance / HILL_RADIUS_DEG, 0.0, 1.0)


def tile_coordinates(zoom: int, x: int, y: int):
    """Latitudes and longitudes of every pixel centre in one tile."""
    span = TILE_PX * (2 ** zoom)
    px = x * TILE_PX + np.arange(TILE_PX) + 0.5
    py = y * TILE_PX + np.arange(TILE_PX) + 0.5
    lons = px / span * 360.0 - 180.0
    lats = np.degrees(np.arctan(np.sinh(math.pi * (1.0 - 2.0 * py / span))))
    return lats[:, None], lons[None, :]


class FakeService:
    """A stand-in for OpenZenith that serves the synthetic hill.

    ``surface`` may be replaced to test empty coverage; ``fail_times`` and
    ``point_fail_times`` make the first N tile or point requests drop, the way the
    real service does under a burst or a slow TLS handshake.
    """

    def __init__(self, surface=hill_elevation, fail_times: int = 0, point_fail_times: int = 0):
        self.surface = surface
        self.fail_times = fail_times
        self.point_fail_times = point_fail_times
        self.tile_requests = 0
        self.point_requests = 0
        self.search_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")

        if parts[:2] == ["api", "tile"]:
            self.tile_requests += 1
            if self.tile_requests <= self.fail_times:
                raise httpx.ConnectError("connection reset by peer")
            zoom, x, y = (int(p) for p in parts[2:5])
            lats, lons = tile_coordinates(zoom, x, y)
            values = np.broadcast_to(self.surface(lats, lons), (TILE_PX, TILE_PX))
            return httpx.Response(200, content=np.round(values).astype("<i2").tobytes())

        if parts[:2] == ["api", "geocode"]:
            self.search_requests += 1
            query = request.url.params["query"].strip().lower()
            matches = [p for p in PLACES if query in p["display_name"].lower()]
            limit = int(request.url.params.get("limit", 5))
            return httpx.Response(200, json={"results": matches[:limit], "count": len(matches)})

        if parts[:2] == ["api", "elevation"]:
            self.point_requests += 1
            if self.point_requests <= self.point_fail_times:
                raise httpx.ConnectTimeout("handshake operation timed out")
            lat = float(request.url.params["lat"])
            lon = float(request.url.params["lon"])
            elevation = float(np.round(self.surface(lat, lon)))
            if elevation <= elevation_api.NODATA + 1:
                return httpx.Response(200, json={"elevation": None, "unit": "meters"})
            return httpx.Response(200, json={
                "elevation": elevation, "unit": "meters", "resolution": 30, "source": "ozt2",
                # The real service merges land elevation with ocean bathymetry, and
                # labels the deep ocean "seafloor" — not "ocean", as its schema says.
                "surface_type": "land" if elevation > -1000 else "seafloor",
            })

        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def dem_service(monkeypatch, tmp_path):
    """Install a fake elevation service, with a tile cache that dies with the test.

    Returns the installer, so a test that needs different ground — no data, dead
    flat, a flaky connection — asks for it rather than repeating the wiring.
    """
    def install(surface=hill_elevation, fail_times: int = 0, point_fail_times: int = 0) -> FakeService:
        fake = FakeService(surface=surface, fail_times=fail_times, point_fail_times=point_fail_times)
        monkeypatch.setattr(settings, "ELEVATION_CACHE_DIR", tmp_path / "dem_cache")
        monkeypatch.setattr(
            elevation_api, "_client",
            lambda: httpx.Client(base_url="http://elevation.test",
                                 transport=httpx.MockTransport(fake.handler)),
        )
        return fake

    return install


@pytest.fixture
def service(dem_service) -> FakeService:
    """The usual fake service: one hill, answering every request."""
    return dem_service()


def flat_ground(height_m: float):
    """A surface with no relief at all, for testing that it is refused."""
    return lambda lat, lon: np.full(np.shape(np.asarray(lat) * np.asarray(lon)), height_m)


def no_data(lat, lon):
    """A surface the service has no readings for."""
    return np.full(np.shape(np.asarray(lat) * np.asarray(lon)), elevation_api.NODATA)


def seabed(lat, lon):
    """Ocean floor: real relief, kilometres below sea level."""
    return -3000.0 + 400.0 * np.sin(np.asarray(lat) * 60.0) * np.cos(np.asarray(lon) * 60.0)


def below_sea_level(lat, lon):
    """Dry land that happens to sit below sea level, as the Dead Sea shore does."""
    return -420.0 + 30.0 * np.cos(np.asarray(lat) * 400.0) * np.cos(np.asarray(lon) * 400.0)
