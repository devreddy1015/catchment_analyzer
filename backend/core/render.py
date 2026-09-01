"""Render the elevation grid to PNG overlays the map can drape over the terrain.

Three images, all aligned to the grid's bounding box so a web map can place them
directly: a hypsometric tint (low ground green through high ground white), a
hillshade, which is what actually makes the landform readable, and a wash over
the ground the water has already claimed.

The last one exists because an exclusion nobody can see looks like a missing
answer. A river drawn as a line is a line; the ground it floods is an area, and
the difference between them is the whole point — a pond can sit fifty metres from
the channel and still be in the river.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# Hypsometric ramp: low wetland green -> tan -> rock brown -> snow white.
_RAMP = np.array([
    [ 34, 102,  51],
    [124, 160,  76],
    [206, 194, 118],
    [176, 133,  84],
    [140, 106,  86],
    [242, 242, 245],
], dtype=float)

# Sun position for the hillshade, in the cartographic convention (light from
# the north-west, so relief reads correctly to most viewers).
SUN_AZIMUTH_DEG = 315.0
SUN_ALTITUDE_DEG = 45.0


def _colourise(normalised: np.ndarray) -> np.ndarray:
    """Map values in 0-1 onto the elevation ramp, linearly between stops."""
    position = np.clip(normalised, 0.0, 1.0) * (len(_RAMP) - 1)
    lower = np.floor(position).astype(int)
    upper = np.minimum(lower + 1, len(_RAMP) - 1)
    blend = (position - lower)[..., None]
    return _RAMP[lower] * (1.0 - blend) + _RAMP[upper] * blend


def elevation_png(z: np.ndarray, path: Path) -> None:
    """Hypsometric tint of the elevation grid."""
    low, high = float(np.min(z)), float(np.max(z))
    rgb = _colourise((z - low) / max(high - low, 1e-9))
    Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(path, "PNG")


def hillshade_png(z: np.ndarray, cell_size_m: float, path: Path, exaggeration: float = 2.0) -> None:
    """Standard Lambertian hillshade, written as a grey image with soft alpha."""
    dz_dy, dz_dx = np.gradient(z * exaggeration, cell_size_m)
    slope = np.arctan(np.hypot(dz_dx, dz_dy))
    aspect = np.arctan2(-dz_dx, dz_dy)

    azimuth = np.radians(360.0 - SUN_AZIMUTH_DEG + 90.0)
    zenith = np.radians(90.0 - SUN_ALTITUDE_DEG)
    shade = np.cos(zenith) * np.cos(slope) + np.sin(zenith) * np.sin(slope) * np.cos(azimuth - aspect)

    grey = (np.clip(shade, 0.0, 1.0) * 255).astype(np.uint8)
    alpha = (255 - grey) // 2 + 60  # dark slopes stay opaque, lit ground goes sheer
    Image.fromarray(np.dstack([grey, grey, grey, alpha]), mode="RGBA").save(path, "PNG")


def prune(directory: Path, keep: int) -> None:
    """Drop the oldest overlays so generated images do not pile up forever."""
    files = sorted(directory.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)


# The wash over ground the water uses. Deliberately not the blue of the drainage
# lines: those are where water runs, this is where it stands.
_WATERCOURSE_COLOURS = {
    "river": (27, 79, 138, 150),
    "floodplain": (60, 120, 175, 80),
    "nala": (90, 140, 120, 110),
    "still_water": (20, 60, 110, 170),
}


def watercourse_png(masks: dict[str, np.ndarray], path: Path) -> None:
    """Ground withheld from pond siting, as a translucent wash.

    Painted least specific first, so a nala drawn over its own floodplain and a
    river drawn over both come out on top — the same order in which the classes
    override one another when a single cell is named.
    """
    shape = next(iter(masks.values())).shape
    rgba = np.zeros((*shape, 4), dtype=np.uint8)

    for name in ("floodplain", "nala", "still_water", "river"):
        mask = masks.get(name)
        if mask is None or not mask.any():
            continue
        rgba[mask] = _WATERCOURSE_COLOURS[name]

    Image.fromarray(rgba, mode="RGBA").save(path, "PNG")
