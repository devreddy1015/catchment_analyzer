# Catchment Analyzer — Submission Report

**Repository:** `<add your GitHub URL here>`
**Deployed API:** `<add your deployed URL here>` — locally `http://localhost:5000`

| | |
|---|---|
| **Primary route** | `POST /analyzeContour` |
| **Alias** | `POST /findCatchment` |
| **Without a file** | `POST /analyzeArea` — coordinates instead of an upload |
| **Finding those coordinates** | `GET /places?q=durg`, `GET /elevation?lat=&lon=` |
| **Also served under** | `/api/…`, e.g. `/api/analyzeContour` |
| **Accepts** | `multipart/form-data` with a `.kml` or `.kmz` contour map, or JSON with a latitude, longitude and area |
| **Returns** | `application/json` — terrain, pond site, catchment, GeoJSON geometry |
| **Interactive docs** | `/docs` |

Full API reference and setup instructions are in [README.md](README.md).

---

## 1. Approach

A contour map is a set of lines that each mark one height. Catchment area is a
property of a *surface*. So the work is: get from lines to a surface, route water
over it, and measure what drains where.

**Contours → surface.** Every contour vertex is a control point at a known height.
A Delaunay triangulation interpolates linearly between them, which reproduces the
original elevations exactly along the contour lines themselves. Cells beyond the
surveyed area are filled by nearest neighbour, and the response reports what
fraction that was, so an extrapolated answer is never passed off as a measured one.

Grid rows and columns follow the ground's aspect ratio, so a cell is square in
metres — which is what the flow model assumes.

**Coordinates → surface.** Most ground has no contour survey, so the same grid can
be filled from [OpenZenith](https://openzenith.org), a free global elevation service
needing no key. Its terrain tiles are 256×256 rasters of int16 metres on the
web-mercator pyramid, so a study area is a handful of requests rather than tens of
thousands of point lookups; the tiles are mosaicked and resampled onto exactly the
grid the contour path would have produced for the same extent. From there the two
routes are indistinguishable — the pipeline takes an `ElevationGrid` and does not
ask where it came from.

**Surface → drainage.** Pits are filled by priority flood, tilted very slightly so
water can cross the flats that filling creates. Each cell then drains to its
steepest downhill neighbour of eight (D8), and totalling cells from the highest
down gives how many cells drain through each one. The fill depths are kept
separately: how much fill a cell needed is how deep a hollow sits there.

**Drainage → pond site.** Cells are scored on depression depth (0.30), upstream
catchment (0.30), slope (0.25) and relative elevation (0.15), each normalised
against the range present in the terrain. Candidates are taken best-first with a
minimum spacing so the result is a set of real alternatives.

Ground the water has already claimed is removed first, classed by upstream
catchment area in hectares: under 25 ha is farm-pond ground, 25–100 ha is a nala
bund or percolation tank, and over 100 ha — plus 40 m either side — is river, on
which nothing is proposed. Standing water is excluded too, detected as a patch
that is flat, level to the source's own vertical step, and larger than any pond.

Size is only half of the test. A site can be two hundred metres from the channel
and still be in the river, because what floods is the valley floor. So each cell
also carries its height above nearest drainage (HAND — follow the flow pointers
downstream to the first channel they meet, take the elevation difference), and
ground under 3 m above its river, or 1 m above its nala, is withheld as
floodplain. On the sample survey the flow model found 140 channel cells, while
the ground lying within a metre of their level was a fifth of the map — and all
five recommended sites sat in it, at the 0th to 8th elevation percentile. None
was on the channel; all were at the channel's elevation. With the height rule the
same five move to the 8th–20th percentile and stand 2.1–5.7 m above the water.
The test is not applied to the channels themselves, since a nala is level with
the river it runs into by construction.

The classification is by size rather than by depth because a wide river reads as a
*deep* trench: the sensor returns its water surface, not its bed, and noise,
bridges and bank canopy cut into it. A rule of "high flow but no hollow" therefore
exempts precisely the rivers it exists to catch — measured over the Rio Negro, 35%
of the main-stem cells escaped it, and four of five recommendations landed on the
river, one of them rated Excellent at the 99.8th flow percentile. Nor can the
threshold be a percentile: that calls a fixed share of every map a channel,
whatever is on it. Hectares mean the same thing everywhere.

The depth rule survives one layer down, for small channels below the nala
threshold: a gully with a lot of flow and no hollow deeper than the source could
record is interpolation, not ground, and on a busy channel it is what smoothing
manufactures. The bar is the terrain's own vertical resolution — the contour
interval, or one metre for integer-metre DEM tiles.

Nothing is dropped silently. Nala-class sites are ranked separately and returned
as the structure they take; the excluded river is drawn on the map. A hollow that
one ordinary storm (50 mm, rational method) would fill and overtop is moved into
that same list, rather than being ranked first as a pond with a caution saying it
is really a nala bund.

What no elevation model can settle is whether a channel runs all year: a perennial
river and a monsoon nala are the same trench in the data. Knowing a river by name
would need hydrography — OSM waterways, HydroSHEDS, India-WRIS — which this does
not use.

**Pond site → catchment.** Walking the flow pointers upstream from the site
collects every contributing cell. Area is the cell count times cell area; the
boundary is those cells merged into one polygon. The longest drainage path gives
the time of concentration by Kirpich's formula, and the rational method
`V = P × A × C` gives runoff yield.

## 2. Demonstration — `contours_1m.kml`

```bash
curl -X POST http://localhost:5000/api/analyzeContour \
  -F "file=@contours_1m.kml" \
  -F "rainfall_mm=1150"
```

Runs in about one second.

**What was read from the file**

| | |
|---|---|
| Contour lines | 1,355 (159,113 vertices) |
| Elevation levels | 32, at a 1 m interval |
| Elevation range | 267 – 298 m |
| Extent | 21.2398 – 21.2636 N, 81.2814 – 81.3126 E |

One placemark was ignored and reported in `warnings`: a boundary polygon named
`land` carrying an altitude of 30 m, which is not part of the contour series.

**Terrain reconstructed** — 131 × 160 grid at 20.6 m cells, 8.9 km² mapped,
29.1 m of relief, 2.4° mean slope, 7.6 % of cells extrapolated.

**Recommended pond site** — 21.244245 N, 81.288307 E · score 77.1 (Excellent)

| | |
|---|---|
| Ground level | 270.53 m, on a 0.32° slope |
| Natural hollow | 3.74 m deep, spilling at 274.27 m |
| Storage at spill | 295,922 m² surface, ≈ 598,720 m³ |

**Catchment**

| | |
|---|---|
| Area | 402.74 ha (4.03 km²) — 45 % of the mapped area |
| Perimeter | 15.9 km |
| Relief | 28.8 m, mean slope 2.5° |
| Longest flow path | 5,082 m |
| Time of concentration | 102 min |
| Yield | 1,611 m³ per mm of rainfall → 1,852,619 m³ at 1,150 mm, C = 0.4 |

Four alternative sites are returned alongside. All geometry — catchment boundary,
site points, longest flow path, drainage lines — comes back as one GeoJSON
`FeatureCollection` that loads directly into QGIS or Leaflet.

## 3. Demonstration — no file at all

```bash
curl -X POST http://localhost:5000/api/analyzeArea \
  -H "Content-Type: application/json" \
  -d '{"latitude": 18.52, "longitude": 73.75, "area_km": 5, "rainfall_mm": 700, "runoff_coefficient": 0.3}'
```

Five kilometres of the Deccan plateau, downloaded in 4 terrain tiles at 18 m
sampling and analysed in about two seconds:

| | |
|---|---|
| Terrain | 597 – 813 m, 216 m of relief, 8.2° mean slope |
| Recommended site | 18.52531 N, 73.72856 E · score 95.9 (Excellent) |
| Natural hollow | 3.6 m deep, spilling at 607.32 m, holding ≈ 72,833 m³ |
| Catchment | 243.5 ha — 9.7 % of the area, yielding 511,318 m³ at 700 mm |

The same request over the sample map's own footprint returns 267 – 297 m against
the surveyed 267 – 298 m, which is the check that the tile arithmetic is reading
the ground it claims to be reading; it runs as a test.

Every area analysis carries its own caveats in `warnings`: the sampling distance,
any gaps that had to be filled, and that the source is a *surface* model in which
canopy and rooftops sit as ground.

Nobody knows the latitude of their own village, so `GET /places` turns a name into
coordinates and the interface keeps the map on screen throughout: the pin and the
area it will cover are drawn before the analysis is asked for, and can be moved by
searching, clicking the map, or typing. The whole interface works in light and dark,
and the basemap follows.

## 4. Extensibility

Each stage is a plain function over plain data, sequenced by `core/pipeline.py`:

```
kml.py ──────────→ grid.py ──┐
                             ├→ hydrology.py → siting.py → pipeline.py
elevation_api.py ────────────┘
```

The elevation service is the demonstration of this rather than an exception to it:
adding it took one new module returning an `ElevationGrid` and one new entry point,
and not one line of the hydrology, siting or rendering changed. A GeoTIFF DEM or an
LAS point cloud would go in the same way. Changing what counts as a good pond site
means editing the weights in `siting.py`. Adding a new output block means adding a
model in `models.py`.

Nothing in the code depends on the sample map. Extent, elevation range, contour
interval and grid shape are all derived from whatever file arrives, and every score
is normalised against the range present in that terrain. The test suite enforces
this by running the same assertions against a synthetic contour map generated in a
different hemisphere, at a different scale, with a different contour interval.

## 5. Verification

```bash
.venv/bin/python -m pytest    # 106 tests, none of which need the network
```

Covering elevation recovery from all four KML encodings, KMZ handling, malformed
input, grid geometry, sink filling, D8 routing on known surfaces, catchment
containment, site spacing, every route, error codes, run-to-run determinism, and
that two different maps give two different answers.

For the elevation service: web-mercator arithmetic, zoom selection, tile coverage,
disk caching, retry on a dropped connection, place-name search, and refusal of ground
that is featureless, underwater, or has no data. A stand-in service answers tile requests with a hill computed from
each pixel's real latitude and longitude, so the downloaded grid can be compared
against a surface that is known analytically — which is what catches a projection
that is subtly shifted or scaled. One test calls the live service and is skipped
unless `ELEVATION_LIVE_TESTS=1`.

## 6. Limitations

Planning estimates from terrain geometry, not an engineering design. No soil, land
use, groundwater or existing-infrastructure data is considered. The contour
interval sets the vertical resolution — features shallower than it are invisible.
Sites must be confirmed on the ground.

Downloaded terrain warrants more caution than a survey. It is a surface model at
about 30 m, so tree canopy and rooftops sit in it as if they were ground, and a
hollow it shows may be a clearing. It is good for finding ground worth walking to,
not for deciding how much to dig — and the response says so on every area analysis.
