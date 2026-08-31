"""Read contour lines out of a KML or KMZ file.

A contour map is a set of LineStrings, each tagged with one elevation. Producers
disagree about *where* that elevation is written, so we try the common places in
order of reliability and take the first plausible number:

1. the Placemark ``<name>``            (e.g. ``<name>277.0</name>``)
2. ``ExtendedData`` fields whose name mentions elevation/altitude/height/level
3. any purely numeric ``ExtendedData`` field
4. the Z component of the coordinates   (``lon,lat,alt``)

Nothing here is specific to any one file: no fixed extents, elevations, or
counts. Placemarks whose elevation cannot be recovered are skipped and counted,
and the count is surfaced to the caller as a warning.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator

# Elevations outside this band are treated as parse noise rather than terrain.
MIN_PLAUSIBLE_ELEVATION_M = -500.0
MAX_PLAUSIBLE_ELEVATION_M = 9000.0

_ELEVATION_KEYS = ("elev", "alt", "height", "level", "contour", "z")
_NUMERIC = re.compile(r"^[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?$")

# A level is "isolated" when the nearest other level is this many intervals away
# and the level holds no more than this share of all contour lines.
ISOLATION_GAP_FACTOR = 5.0
ISOLATION_MAX_SHARE = 0.02


class ContourParseError(ValueError):
    """Raised when a file cannot be read as a usable contour map."""


@dataclass(frozen=True)
class ContourLine:
    elevation_m: float
    coordinates: list[tuple[float, float]]  # (lon, lat)

    @property
    def is_closed(self) -> bool:
        first, last = self.coordinates[0], self.coordinates[-1]
        return len(self.coordinates) >= 4 and abs(first[0] - last[0]) < 1e-9 and abs(first[1] - last[1]) < 1e-9


@dataclass
class ContourSet:
    """All contour lines from one file, plus the metadata derived from them."""

    lines: list[ContourLine]
    source_format: str = "KML"
    skipped_placemarks: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def levels(self) -> list[float]:
        return sorted({line.elevation_m for line in self.lines})

    @property
    def elevation_range(self) -> tuple[float, float]:
        levels = self.levels
        return levels[0], levels[-1]

    @property
    def interval_m(self) -> float:
        """Median gap between neighbouring elevation levels."""
        levels = self.levels
        gaps = sorted(b - a for a, b in zip(levels, levels[1:]))
        return round(gaps[len(gaps) // 2], 4) if gaps else 0.0

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(south, west, north, east) in degrees."""
        lons = [lon for line in self.lines for lon, _ in line.coordinates]
        lats = [lat for line in self.lines for _, lat in line.coordinates]
        return min(lats), min(lons), max(lats), max(lons)

    @property
    def vertex_count(self) -> int:
        return sum(len(line.coordinates) for line in self.lines)


# --------------------------------------------------------------------------- #
# XML helpers — KML files use several namespaces, so we compare local names.
# --------------------------------------------------------------------------- #

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in element.iter():
        if child is not element and _local(child.tag) == name:
            yield child


def _as_elevation(text: str | None) -> float | None:
    if not text or not _NUMERIC.match(text.strip()):
        return None
    value = float(text.strip())
    if MIN_PLAUSIBLE_ELEVATION_M <= value <= MAX_PLAUSIBLE_ELEVATION_M:
        return value
    return None


def _parse_coordinates(text: str | None) -> tuple[list[tuple[float, float]], float | None]:
    """Parse a KML ``<coordinates>`` blob into (lon, lat) pairs.

    Returns the pairs plus the first altitude found, which acts as a last-resort
    elevation source.
    """
    points: list[tuple[float, float]] = []
    altitude: float | None = None
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            continue
        points.append((lon, lat))
        if altitude is None and len(parts) >= 3:
            altitude = _as_elevation(parts[2])
    return points, altitude


def _placemark_elevation(placemark: ET.Element, coord_altitude: float | None) -> float | None:
    for name_el in _children(placemark, "name"):
        value = _as_elevation(name_el.text)
        if value is not None:
            return value

    fields = list(_children(placemark, "SimpleData")) + list(_children(placemark, "Data"))
    for field_el in fields:
        key = (field_el.get("name") or "").lower()
        text = field_el.text if field_el.text and field_el.text.strip() else None
        if text is None:  # <Data><value>…</value></Data>
            value_el = next(_children(field_el, "value"), None)
            text = value_el.text if value_el is not None else None
        if any(k in key for k in _ELEVATION_KEYS):
            value = _as_elevation(text)
            if value is not None:
                return value

    for field_el in fields:
        value = _as_elevation(field_el.text)
        if value is not None:
            return value

    return coord_altitude



def _drop_isolated_levels(lines: list[ContourLine]) -> tuple[list[ContourLine], list[str]]:
    """Remove elevation levels that clearly do not belong to the contour series.

    Contour maps step through elevation at a regular interval. A level that sits
    far from every other level *and* carries only a sliver of the lines is
    almost always an artefact — a map frame, a legend box, an annotation whose
    name happens to be numeric. Keeping one would punch a false sink or peak
    into the reconstructed terrain, so it is dropped and reported.
    """
    levels = sorted({line.elevation_m for line in lines})
    if len(levels) < 4:
        return lines, []

    gaps = sorted(b - a for a, b in zip(levels, levels[1:]))
    interval = gaps[len(gaps) // 2] or 1.0
    max_gap = ISOLATION_GAP_FACTOR * interval

    counts: dict[float, int] = {}
    for line in lines:
        counts[line.elevation_m] = counts.get(line.elevation_m, 0) + 1

    dropped = {
        level
        for i, level in enumerate(levels)
        if min(
            (level - levels[i - 1]) if i > 0 else float("inf"),
            (levels[i + 1] - level) if i + 1 < len(levels) else float("inf"),
        ) > max_gap
        and counts[level] <= ISOLATION_MAX_SHARE * len(lines)
    }
    if not dropped:
        return lines, []

    kept = [line for line in lines if line.elevation_m not in dropped]
    listed = ", ".join(f"{level:g} m" for level in sorted(dropped)[:5])
    plural = "level" if len(dropped) == 1 else "levels"
    return kept, [
        f"Ignored {len(dropped)} isolated elevation {plural} ({listed}) that did not fit "
        f"the {interval:g} m contour interval."
    ]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def extract_kml_from_kmz(data: bytes) -> bytes:
    """Pull the main KML document out of a KMZ (zip) archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".kml")]
            if not members:
                raise ContourParseError("KMZ archive contains no .kml document.")
            preferred = next((n for n in members if n.lower().endswith("doc.kml")), members[0])
            return archive.read(preferred)
    except zipfile.BadZipFile as exc:
        raise ContourParseError(f"KMZ file is not a valid archive: {exc}") from exc


def parse_kml(data: bytes) -> ContourSet:
    """Parse KML bytes into a :class:`ContourSet`."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ContourParseError(f"File is not well-formed XML: {exc}") from exc

    lines: list[ContourLine] = []
    skipped = 0

    for placemark in root.iter():
        if _local(placemark.tag) != "Placemark":
            continue

        geometries = [g for g in placemark.iter() if _local(g.tag) in ("LineString", "LinearRing")]
        if not geometries:
            continue  # points, polygons and labels are not contours

        recovered = False
        for geometry in geometries:
            for coord_el in _children(geometry, "coordinates"):
                points, altitude = _parse_coordinates(coord_el.text)
                if len(points) < 2:
                    continue
                elevation = _placemark_elevation(placemark, altitude)
                if elevation is None:
                    continue
                lines.append(ContourLine(elevation_m=elevation, coordinates=points))
                recovered = True
        if not recovered:
            skipped += 1

    if not lines:
        raise ContourParseError(
            "No contour lines with a recoverable elevation were found. Elevations are "
            "read from the Placemark <name>, from ExtendedData, or from the Z value of "
            "the coordinates."
        )

    lines, level_warnings = _drop_isolated_levels(lines)
    contours = ContourSet(lines=lines, skipped_placemarks=skipped, warnings=level_warnings)
    if skipped:
        contours.warnings.append(
            f"{skipped} placemark(s) had no readable elevation and were ignored."
        )
    return contours


def read_contours(data: bytes, filename: str = "") -> ContourSet:
    """Read a contour map from raw KML or KMZ bytes."""
    if not data:
        raise ContourParseError("Uploaded file is empty.")

    is_kmz = filename.lower().endswith(".kmz") or data[:2] == b"PK"
    contours = parse_kml(extract_kml_from_kmz(data) if is_kmz else data)
    contours.source_format = "KMZ" if is_kmz else "KML"
    return contours


def validate(contours: ContourSet, min_lines: int = 3, min_levels: int = 2) -> None:
    """Reject contour sets too sparse or too degenerate to build terrain from."""
    if len(contours.lines) < min_lines:
        raise ContourParseError(
            f"Only {len(contours.lines)} contour line(s) found; at least {min_lines} are needed."
        )
    if len(contours.levels) < min_levels:
        raise ContourParseError(
            f"Only {len(contours.levels)} distinct elevation level(s) found; "
            f"at least {min_levels} are needed to model a slope."
        )
    south, west, north, east = contours.bounds
    if (north - south) < 1e-6 or (east - west) < 1e-6:
        raise ContourParseError("Contours cover a negligible area; the coordinates look degenerate.")
