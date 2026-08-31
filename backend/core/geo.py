"""Geodesic helpers.

The analysis works in geographic coordinates (EPSG:4326) but reports distances
and areas in metres, so every conversion between the two lives here.
"""
from __future__ import annotations

import math
from typing import Sequence

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def metres_per_degree(lat: float) -> tuple[float, float]:
    """Local scale factors (metres per degree of latitude, of longitude)."""
    lat_rad = math.radians(lat)
    m_per_lat = 111_132.92 - 559.82 * math.cos(2 * lat_rad) + 1.175 * math.cos(4 * lat_rad)
    m_per_lon = 111_412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)
    return m_per_lat, max(1.0, m_per_lon)


def bbox_around(lat: float, lon: float, side_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a square of ground ``side_m`` across, centred
    on a point. Degrees of longitude shorten towards the poles, so the two half
    widths are computed separately and the box stays square on the ground."""
    m_lat, m_lon = metres_per_degree(lat)
    half_lat = side_m / 2.0 / m_lat
    half_lon = side_m / 2.0 / m_lon
    return (
        max(-89.0, lat - half_lat),
        max(-180.0, lon - half_lon),
        min(89.0, lat + half_lat),
        min(180.0, lon + half_lon),
    )


def ring_area_m2(ring: Sequence[Sequence[float]]) -> float:
    """Area of a closed (lon, lat) ring, via the shoelace formula on a local
    equirectangular projection centred on the ring itself."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    m_lat, m_lon = metres_per_degree(lat0)
    xy = [((p[0] - ring[0][0]) * m_lon, (p[1] - ring[0][1]) * m_lat) for p in ring]
    twice = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]):
        twice += x1 * y2 - x2 * y1
    return abs(twice) / 2.0


def path_length_m(points: Sequence[Sequence[float]]) -> float:
    """Total length of an open (lon, lat) polyline, in metres."""
    return sum(
        haversine_m(a[1], a[0], b[1], b[0]) for a, b in zip(points, points[1:])
    )
