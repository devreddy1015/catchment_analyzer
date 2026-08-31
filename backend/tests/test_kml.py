"""Reading contour lines out of KML and KMZ."""
from __future__ import annotations

import pytest

from backend.core import kml

from .conftest import make_kml


def test_reads_sample_map(sample_kml: bytes) -> None:
    contours = kml.read_contours(sample_kml, "contours_1m.kml")
    kml.validate(contours)

    assert contours.source_format == "KML"
    assert len(contours.lines) > 100
    assert contours.interval_m == pytest.approx(1.0)
    south, west, north, east = contours.bounds
    assert south < north and west < east


def test_reads_kmz(synthetic_kmz: bytes) -> None:
    contours = kml.read_contours(synthetic_kmz, "hill.kmz")
    assert contours.source_format == "KMZ"
    assert len(contours.lines) == 20


def test_detects_kmz_without_extension(synthetic_kmz: bytes) -> None:
    """A KMZ is recognised by its zip signature even if renamed."""
    assert kml.read_contours(synthetic_kmz, "mystery.kml").source_format == "KMZ"


def test_interval_is_derived_not_assumed(synthetic_kml: bytes) -> None:
    assert kml.read_contours(synthetic_kml).interval_m == pytest.approx(5.0)


def test_elevation_from_coordinate_z() -> None:
    """Placemarks with no name fall back to the Z component of the coordinates."""
    document = (
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><LineString><coordinates>10.0,20.0,150 10.01,20.0,150</coordinates>"
        "</LineString></Placemark>"
        "<Placemark><LineString><coordinates>10.0,20.01,160 10.01,20.01,160</coordinates>"
        "</LineString></Placemark>"
        "</Document></kml>"
    ).encode()
    assert kml.read_contours(document).levels == [150.0, 160.0]


def test_elevation_from_extended_data() -> None:
    document = (
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>Contour A</name><ExtendedData><SchemaData>"
        '<SimpleData name="ELEVATION">42.5</SimpleData></SchemaData></ExtendedData>'
        "<LineString><coordinates>1,1 1.01,1</coordinates></LineString></Placemark>"
        "</Document></kml>"
    ).encode()
    assert kml.read_contours(document).levels == [42.5]


def test_isolated_level_is_dropped() -> None:
    """A stray numeric placemark far from the contour series is ignored."""
    base = make_kml(0.0, 0.0, 0.02, range(1, 21), 1.0).decode()
    stray = (
        "<Placemark><name>9999</name><LineString>"
        "<coordinates>0.001,0.001 0.002,0.002</coordinates></LineString></Placemark>"
    )
    contours = kml.read_contours(base.replace("</Document>", stray + "</Document>").encode())

    assert 9999.0 not in contours.levels
    assert contours.warnings


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "empty"),
        (b"<kml><Document>", "well-formed"),
        (b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>', "No contour lines"),
    ],
)
def test_rejects_unusable_files(payload: bytes, message: str) -> None:
    with pytest.raises(kml.ContourParseError, match=message):
        kml.read_contours(payload, "broken.kml")


def test_validate_rejects_single_level() -> None:
    """Contours all at one height describe no slope, so no terrain can be built."""
    lines = "".join(
        f"<Placemark><name>100</name><LineString>"
        f"<coordinates>0.0{i},0.0 0.0{i},0.01</coordinates></LineString></Placemark>"
        for i in range(1, 5)
    )
    document = f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{lines}</Document></kml>'.encode()
    with pytest.raises(kml.ContourParseError, match="elevation level"):
        kml.validate(kml.read_contours(document))
