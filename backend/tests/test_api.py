"""The HTTP surface, end to end."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.core.geo import bbox_around

from .conftest import HILL_BASE_M, HILL_HEIGHT_M, HILL_LAT, HILL_LON, no_data

ENDPOINTS = ["/api/analyzeContour", "/api/findCatchment", "/analyzeContour", "/findCatchment"]


def post_map(client: TestClient, payload: bytes, name: str = "contours.kml",
             endpoint: str = "/api/analyzeContour", **fields):
    return client.post(
        endpoint,
        files={"file": (name, payload, "application/vnd.google-earth.kml+xml")},
        data={k: str(v) for k, v in fields.items()},
    )


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_root_lists_endpoints(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["endpoints"]["analyze"] == "/api/analyzeContour"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_every_route_answers(client: TestClient, synthetic_kml: bytes, endpoint: str) -> None:
    """Both names work, with and without the /api prefix."""
    response = post_map(client, synthetic_kml, endpoint=endpoint, resolution=60)
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True


def test_sample_map_analysis(client: TestClient, sample_kml: bytes) -> None:
    response = post_map(client, sample_kml, "contours_1m.kml", resolution=120, rainfall_mm=1100)
    assert response.status_code == 200, response.text
    body = response.json()

    source, terrain, site, catchment = (
        body["source"], body["terrain"], body["pond_site"], body["catchment"]
    )

    assert source["format"] == "KML"
    assert source["contour_lines"] > 100
    assert source["contour_interval_m"] == pytest.approx(1.0)

    # The pond site must sit inside the terrain the file described.
    bounds = terrain["bounds"]
    assert bounds["south"] <= site["latitude"] <= bounds["north"]
    assert bounds["west"] <= site["longitude"] <= bounds["east"]

    # The catchment must be real but cannot exceed the map.
    assert 0 < catchment["area_m2"] <= terrain["mapped_area_km2"] * 1e6
    cells = terrain["grid"]["rows"] * terrain["grid"]["cols"]
    assert catchment["share_of_map"] == pytest.approx(catchment["cell_count"] / cells, abs=1e-4)
    assert catchment["area_m2"] == pytest.approx(
        catchment["cell_count"] * terrain["grid"]["cell_area_m2"], rel=1e-3
    )
    assert catchment["longest_flow_path_m"] > 0
    assert catchment["time_of_concentration_min"] > 0
    assert catchment["runoff"]["runoff_m3"] == pytest.approx(
        catchment["runoff"]["yield_m3_per_mm"] * 1100, rel=1e-3
    )

    assert body["overlays"]["elevation"].endswith(".png")
    assert client.get(body["overlays"]["elevation"]).status_code == 200


def test_geojson_is_wellformed(client: TestClient, synthetic_kml: bytes) -> None:
    body = post_map(client, synthetic_kml, resolution=60).json()
    collection = body["geojson"]

    assert collection["type"] == "FeatureCollection"
    kinds = {f["properties"]["type"] for f in collection["features"]}
    assert {"catchment", "pond_site"} <= kinds

    boundary = next(f for f in collection["features"] if f["properties"]["type"] == "catchment")
    ring = boundary["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) >= 4


def test_results_are_not_tied_to_the_sample_map(client: TestClient, synthetic_kml: bytes,
                                                sample_kml: bytes) -> None:
    """A different map in a different hemisphere must give a different answer."""
    other = post_map(client, synthetic_kml, "hill.kml", resolution=80).json()
    sample = post_map(client, sample_kml, "contours_1m.kml", resolution=80).json()

    assert other["pond_site"]["latitude"] < 0 < sample["pond_site"]["latitude"]
    assert other["source"]["contour_interval_m"] != sample["source"]["contour_interval_m"]
    assert other["catchment"]["area_m2"] != sample["catchment"]["area_m2"]


def test_repeated_runs_agree(client: TestClient, synthetic_kml: bytes) -> None:
    """Same file, same options, same numbers — nothing random in the pipeline."""
    runs = [post_map(client, synthetic_kml, resolution=70).json() for _ in range(2)]
    for field in ("latitude", "longitude", "score"):
        assert runs[0]["pond_site"][field] == runs[1]["pond_site"][field]
    assert runs[0]["catchment"]["area_m2"] == runs[1]["catchment"]["area_m2"]


def test_kmz_upload(client: TestClient, synthetic_kmz: bytes) -> None:
    body = post_map(client, synthetic_kmz, "hill.kmz", resolution=60).json()
    assert body["source"]["format"] == "KMZ"


def test_options_change_the_result(client: TestClient, synthetic_kml: bytes) -> None:
    coarse = post_map(client, synthetic_kml, resolution=60, max_sites=2).json()
    fine = post_map(client, synthetic_kml, resolution=140, max_sites=4).json()

    assert coarse["terrain"]["grid"]["cols"] < fine["terrain"]["grid"]["cols"]
    assert len(coarse["alternative_sites"]) == 1
    assert len(fine["alternative_sites"]) == 3


def test_resolution_is_clamped(client: TestClient, synthetic_kml: bytes) -> None:
    from backend.config import settings

    body = post_map(client, synthetic_kml, resolution=100_000).json()
    assert body["options"]["resolution"] == settings.MAX_RESOLUTION


def test_rainfall_is_optional(client: TestClient, synthetic_kml: bytes) -> None:
    runoff = post_map(client, synthetic_kml, resolution=60).json()["catchment"]["runoff"]
    assert runoff["runoff_m3"] is None
    assert runoff["yield_m3_per_mm"] > 0


def test_warnings_surface_dropped_data(client: TestClient, sample_kml: bytes) -> None:
    body = post_map(client, sample_kml, "contours_1m.kml", resolution=60).json()
    assert any("isolated elevation" in w for w in body["warnings"])


@pytest.mark.parametrize(
    ("name", "payload", "status"),
    [
        ("terrain.tif", b"II*\x00 not a kml", 400),
        ("empty.kml", b"", 400),
        ("broken.kml", b"<kml><Document>", 422),
        ("flat.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>', 422),
    ],
)
def test_bad_uploads_are_rejected(client: TestClient, name: str, payload: bytes, status: int) -> None:
    response = post_map(client, payload, name)
    assert response.status_code == status
    assert response.json()["detail"]


def test_missing_file_is_rejected(client: TestClient) -> None:
    assert client.post("/api/analyzeContour").status_code == 422


# --------------------------------------------------------------------------- #
# Analysing an area by coordinates, terrain from the elevation service          #
# --------------------------------------------------------------------------- #

AREA = {"latitude": HILL_LAT, "longitude": HILL_LON, "area_km": 3.0, "resolution": 90}


def test_area_analysis_returns_the_same_shape_of_result(client: TestClient, service) -> None:
    """A downloaded area must answer every question an uploaded map answers."""
    response = client.post("/api/analyzeArea", json={**AREA, "rainfall_mm": 900})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["success"] is True
    assert body["source"]["kind"] == "elevation_service"
    assert body["source"]["provider"] == "OpenZenith"
    assert body["source"]["contour_lines"] is None
    assert body["source"]["sample_spacing_m"] > 0
    assert body["options"]["centre"]["latitude"] == pytest.approx(HILL_LAT)
    assert body["options"]["area_km"] == pytest.approx(3.0)

    # The same downstream blocks as a contour analysis, filled in.
    assert body["catchment"]["area_m2"] > 0
    assert body["catchment"]["runoff"]["runoff_m3"] > 0
    assert body["pond_site"]["rating"]
    assert body["overlays"]["elevation"].startswith("/storage/")
    assert body["geojson"]["features"]


def test_area_analysis_finds_the_hill_it_downloaded(client: TestClient, service) -> None:
    """The result must sit on the synthetic hill, not somewhere the maths drifted to."""
    body = client.post("/api/analyzeArea", json=AREA).json()

    terrain, site = body["terrain"], body["pond_site"]
    assert terrain["max_elevation_m"] == pytest.approx(HILL_BASE_M + HILL_HEIGHT_M, abs=3.0)

    bounds = body["source"]["bounds"]
    assert bounds["south"] <= site["latitude"] <= bounds["north"]
    assert bounds["west"] <= site["longitude"] <= bounds["east"]


def test_area_analysis_says_where_the_terrain_came_from(client: TestClient, service) -> None:
    """Remotely sensed ground is not a survey, and the response has to say so."""
    warnings = client.post("/api/analyzeArea", json=AREA).json()["warnings"]
    assert any("OpenZenith" in w for w in warnings)
    assert any("surface model" in w for w in warnings)


def test_area_can_be_given_as_a_rectangle(client: TestClient, service) -> None:
    """A map viewport is a bounding box, so `bounds` is accepted instead of a centre."""
    south, west, north, east = bbox_around(HILL_LAT, HILL_LON, 2500.0)
    body = client.post("/api/analyzeArea", json={
        "bounds": {"south": south, "west": west, "north": north, "east": east},
        "resolution": 80,
    }).json()

    assert body["source"]["bounds"]["north"] == pytest.approx(north)
    assert body["options"]["area_km"] == pytest.approx(2.5, rel=0.02)


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({**AREA, "area_km": 500}, "largest"),
        ({**AREA, "area_km": 0.05}, "smallest"),
        # An emptied "Area" box sends 0. Silently analysing the default instead
        # would report a size the caller never asked for.
        ({**AREA, "area_km": 0}, "smallest"),
        ({**AREA, "area_km": -5}, "smallest"),
        ({"resolution": 80}, "latitude and longitude"),
        ({"bounds": {"south": 5.0, "west": 5.0, "north": 4.0, "east": 6.0}}, "empty"),
        ({"bounds": {"south": 5.0, "west": 5.0, "north": 5.0, "east": 5.0}}, "empty"),
        ({"bounds": {"south": -80.0, "west": -180.0, "north": 80.0, "east": 180.0}}, "largest"),
    ],
)
def test_unusable_areas_are_rejected(client: TestClient, service, payload: dict, fragment: str) -> None:
    response = client.post("/api/analyzeArea", json=payload)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


def test_area_defaults_when_no_size_is_given(client: TestClient, service) -> None:
    """Absent is absent — only *no* size falls back to the default."""
    body = client.post("/api/analyzeArea", json={
        "latitude": HILL_LAT, "longitude": HILL_LON, "resolution": 80,
    }).json()
    assert body["options"]["area_km"] == pytest.approx(2.5)


def test_a_failing_service_is_reported_as_a_bad_gateway(client: TestClient, dem_service) -> None:
    """The service being down is not this service being broken."""
    dem_service(surface=no_data)
    response = client.post("/api/analyzeArea", json=AREA)
    assert response.status_code == 502
    assert "No elevation data" in response.json()["detail"]


def test_elevation_lookup(client: TestClient, service) -> None:
    body = client.get("/api/elevation", params={"lat": HILL_LAT, "lon": HILL_LON}).json()
    assert body["elevation_m"] == pytest.approx(HILL_BASE_M + HILL_HEIGHT_M, abs=1.0)
    assert body["surface_type"] == "land"


def test_elevation_lookup_validates_coordinates(client: TestClient) -> None:
    assert client.get("/api/elevation", params={"lat": 120, "lon": 0}).status_code == 422


def test_place_search(client: TestClient, service) -> None:
    """Typing a place name is how a location gets onto the map."""
    body = client.get("/api/places", params={"q": "durg"}).json()
    assert body[0]["name"].startswith("Durg")
    assert body[0]["latitude"] == pytest.approx(21.1983)


def test_place_search_with_no_match_is_an_empty_list(client: TestClient, service) -> None:
    response = client.get("/api/places", params={"q": "nowhere at all"})
    assert response.status_code == 200
    assert response.json() == []


def test_place_search_needs_something_to_search_for(client: TestClient) -> None:
    assert client.get("/api/places", params={"q": "d"}).status_code == 422
