# Catchment Analyzer

Give it terrain. Get back the best place to put a pond and the area of land that
drains to it.

Terrain arrives two ways, and the analysis after that is identical:

- **Upload a contour map** — a **KML or KMZ** survey. Extent, elevation range,
  contour interval and grid size are all read from the file. Nothing is tied to any
  particular map.
- **Point at a place on the map** — search for it by name, click the map, or type
  coordinates, then say how much ground to cover. Elevations are downloaded from
  [**OpenZenith**](https://openzenith.org), a free global elevation service that
  needs no key, no account and no sign-up. Any land on Earth can be analysed with
  no survey in hand.

```
KML/KMZ ─► contour lines  ─┐
                           ├─► elevation grid ─► fill sinks ─► flow direction
lat/lon ─► OpenZenith DEM ─┘     ─► flow accumulation ─► pond site ranking
                                 ─► catchment ─► JSON
```

---

## Quick start

Requires Python 3.11+ and Node 18+. **Run every command from the project root** —
the one holding this README — not from `backend/`.

```bash
cd /path/to/Pond-Planning-System-main

# Install, once
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend install

# Run
./dev.sh
```

**`./dev.sh` is the one to use**, because there are two servers and both must be
running. The interface on port 3000 is only a front for the API on port 5249; on
its own it can draw the map and nothing else. If you would rather start them by
hand, that is two terminals, both at the project root:

```bash
# terminal 1 — the API
.venv/bin/uvicorn backend.main:app --reload --port 5249

# terminal 2 — the interface
npm --prefix frontend run dev
```

Start only the second and the page will say so, in the panel where errors go.

`backend.main:app` is an import path, so Python must be able to see the `backend`
package — which it can only do from the root. Running these inside `backend/` gives
you `Could not open requirements file` and a `.venv` in the wrong place.

| What | Where |
|---|---|
| Web interface | http://localhost:3000 |
| API | http://localhost:5249/api/analyzeContour |
| Interactive API docs | http://localhost:5249/docs |

`docker compose up --build` runs everything in one container on port 5249, UI
included — one process, so nothing to forget.

Try it straight away with the contour map in this repo:

```bash
curl -X POST http://localhost:5249/api/analyzeContour \
  -F "file=@contours_1m.kml" \
  -F "rainfall_mm=1150"
```

Or with no file at all — 5 km of the Deccan plateau:

```bash
curl -X POST http://localhost:5249/api/analyzeArea \
  -H "Content-Type: application/json" \
  -d '{"latitude": 18.52, "longitude": 73.75, "area_km": 5, "rainfall_mm": 700}'
```

---

## API

### `POST /analyzeContour`

Also available as `POST /findCatchment` — the same endpoint under a second name.
Both work with or without the `/api` prefix, so all four of these are valid:

```
/api/analyzeContour    /api/findCatchment    /analyzeContour    /findCatchment
```

**Request** — `multipart/form-data`

| Field | Type | Default | Notes |
|---|---|---|---|
| `file` | file | *required* | Contour map, `.kml` or `.kmz`, up to 64 MB |
| `resolution` | int | `160` | Grid cells along the longer side, 48–260. Higher is finer but slower |
| `max_sites` | int | `5` | How many candidate pond sites to rank |
| `runoff_coefficient` | float | `0.4` | Rational-method *C*: ~0.15 forest, 0.4 farmland, 0.8 built up |
| `rainfall_mm` | float | — | Optional. Given, the response includes a runoff volume |

**Response** — `200 application/json`

| Block | What it holds |
|---|---|
| `source` | Where the terrain came from — `kind` says which, and the fields follow from that |
| `terrain` | The reconstructed surface: elevation range, relief, slope, grid shape and cell size |
| `pond_site` | The recommended site — position, score, why it scored that way, storage available |
| `catchment` | Area, perimeter, relief, slope, longest flow path, time of concentration, runoff |
| `alternative_sites` | The runners-up, same shape as `pond_site` |
| `overlays` | URLs of the elevation-tint and hillshade PNGs, with the bounds to place them |
| `geojson` | One `FeatureCollection`: catchment boundary, pond sites, flow path, drainage lines |
| `warnings` | Anything ignored or approximated, in plain language |

Abridged, from the sample map:

```jsonc
{
  "success": true,
  "analysis_id": "ca_ed96be4240",
  "source": {
    "kind": "contour_file",
    "name": "contours_1m.kml", "format": "KML",
    "contour_lines": 1355, "elevation_levels": 32,
    "contour_interval_m": 1.0,
    "elevation_min_m": 267.0, "elevation_max_m": 298.0
  },
  "terrain": {
    "relief_m": 29.13, "mean_slope_deg": 2.37, "mapped_area_km2": 8.9,
    "grid": { "rows": 131, "cols": 160, "cell_size_m": 20.6 }
  },
  "pond_site": {
    "latitude": 21.244245, "longitude": 81.288307,
    "elevation_m": 270.53, "slope_deg": 0.32,
    "score": 77.1, "rating": "Excellent",
    "score_breakdown": { "depression": 0.35, "catchment": 0.99, "slope": 0.94, "elevation": 0.90 },
    "storage": { "depth_m": 3.74, "spill_elevation_m": 274.27, "volume_m3": 598719.6 }
  },
  "catchment": {
    "area_hectares": 402.74, "area_km2": 4.0274,
    "perimeter_m": 15894.1, "relief_m": 28.79, "mean_slope_deg": 2.48,
    "longest_flow_path_m": 5082.0, "time_of_concentration_min": 102.0,
    "share_of_map": 0.4526,
    "runoff": { "yield_m3_per_mm": 1610.97, "rainfall_mm": 1150, "runoff_m3": 1852619.3 }
  },
  "warnings": ["Ignored 1 isolated elevation level (30 m) that did not fit the 1 m contour interval."]
}
```

### `POST /analyzeArea`

The same analysis for a place on the map, with no file to upload. Elevations come
from OpenZenith, so this works anywhere there is land.

**Request** — `application/json`

| Field | Type | Default | Notes |
|---|---|---|---|
| `latitude` | float | *required* † | Degrees north, −89 to 89 |
| `longitude` | float | *required* † | Degrees east, −180 to 180 |
| `area_km` | float | `2.5` | Side of the square to analyse, 0.25–12 km |
| `bounds` | object | — | † `{south, west, north, east}` instead of a centre — a map viewport, say |
| `resolution` | int | `160` | Grid cells along the longer side, 48–260 |
| `max_sites` | int | `5` | How many candidate pond sites to rank |
| `runoff_coefficient` | float | `0.4` | Rational-method *C* |
| `rainfall_mm` | float | — | Optional. Given, the response includes a runoff volume |

Before anything is downloaded, one point lookup checks that the middle of the area
is land. The dataset merges ocean bathymetry with land elevation, so the seabed has
a perfectly good surface with hollows in it, and without that check an area over
water comes back with a confident pond site three kilometres down. Land that merely
sits below sea level — the Jordan valley, a polder — is still land, and is analysed
normally.

**Response** — identical to `/analyzeContour`, except that `source` describes the
download rather than a file, and `options` carries the `centre` and `area_km` that
were resolved:

```jsonc
{
  "source": {
    "kind": "elevation_service",
    "name": "18.5200°N 73.7500°E · 5 km across",
    "format": "Copernicus GLO-30 / GEBCO 2025",
    "provider": "OpenZenith",
    "sample_spacing_m": 17.81, "tiles_fetched": 4, "tile_zoom": 13,
    "elevation_min_m": 596.97, "elevation_max_m": 812.8
  },
  "warnings": [
    "Terrain came from OpenZenith (Copernicus GLO-30 / GEBCO 2025) at roughly 18 m sampling, not from a ground survey. Treat the pond site as a place to go and look, not as a design.",
    "The source is a surface model: tree canopy and buildings sit in it as ground, which can invent or hide shallow hollows."
  ]
}
```

### `GET /places?q=`

A place name to coordinates, so an area can be asked for by name rather than by
latitude. Proxied from Nominatim, best match first, up to `limit` results
(default 5, max 10). An empty list means nothing matched, which is not an error.

```bash
curl "http://localhost:5249/api/places?q=durg"
[{"name":"Durg, Chhattisgarh, India","latitude":21.1983,"longitude":81.4008,
  "kind":"administrative"},
 {"name":"Durg, Durg Tahsil, Durg, Chhattisgarh, 491002, India",
  "latitude":21.1896,"longitude":81.2851,"kind":"city"}]
```

### `GET /elevation?lat=&lon=`

One elevation reading, straight through from the service. A single cheap request,
which is what makes it useful — it says whether a point is land with usable data
before you commit to a full area analysis.

```bash
curl "http://localhost:5249/api/elevation?lat=21.2517&lon=81.2970"
{"latitude":21.2517,"longitude":81.297,"elevation_m":273.0,
 "surface_type":"land","resolution_m":30.0,"source":"ozt2"}
```

### Errors

Failures return `{"detail": "<what went wrong>"}` with a status code:

| Code | Meaning |
|---|---|
| `400` | Not a `.kml`/`.kmz` file, or the upload was empty |
| `413` | File over the 64 MB limit |
| `422` | Not well-formed XML, no readable contour elevations, or an unusable area |
| `502` | No usable terrain there — open water, no coverage, no relief, or the service could not be reached |
| `500` | Unexpected failure |

### Other routes

| Route | Purpose |
|---|---|
| `GET /api/health` | Liveness check |
| `GET /docs` | Interactive OpenAPI docs |
| `GET /storage/{id}_elevation.png` | Generated overlay images |

---

## The interface

The map is always on screen, not just after an analysis. In **Map location** mode
it shows a pin and a dashed rectangle for the ground a run would cover, so you can
see what you are about to ask for before you ask for it. Three ways to move the pin,
and they all end in the same place:

| | |
|---|---|
| **Search** | Type `durg` and pick from the matches |
| **Click** | Click anywhere on the map |
| **Type** | Enter latitude and longitude directly |

**Check** reads the elevation under the pin in one request, which is the cheap way
to find out whether somewhere is land with usable data before running a full
analysis over it.

Light and dark both work, and the map follows: the plain basemap switches between
Esri's light and dark canvases so a dark page is not lit up by its own map. The
theme starts by following the operating system and keeps following it; choosing
one from the button in the header overrides that, and is remembered. It is applied
before the first paint, so there is no flash of the wrong theme on load.

### When something is wrong

Every failure says what happened and what to do, and none of them is a stack trace
or a bare status code. The ones worth knowing about:

| Situation | What you get |
|---|---|
| The API is not running | A panel on load naming the command to start it |
| The area is over water | `The middle of that area is water — the service calls it 'seafloor' at −3863 m.` |
| The ground is dead flat | `Flat to within half a metre, so there is nothing for water to run down.` |
| The area is too big or too small | The size you asked for, and the limit |
| A coordinate box is half-typed | The box is marked, and the pin does not move until it parses |
| The elevation service is unreachable | `502`, saying it was the service and not the analysis |
| No contour line has a readable elevation | `422`, naming the four places an elevation is looked for |

A tile request that drops — the service does this under a burst — is retried
rather than surfaced, and a coordinate that is merely below sea level is not
mistaken for the sea.

---

## How the catchment is worked out

**1 · Read the contours.** Each KML `Placemark` holding a `LineString` becomes one
contour line. Producers disagree about where the elevation is written, so four
sources are tried in order: the `<name>`, an `ExtendedData` field mentioning
elevation, any numeric `ExtendedData` field, then the Z value of the coordinates.

A contour series steps through elevation at a regular interval. A level sitting far
from every other level while holding almost none of the lines is an artefact — a map
frame or a stray label — so it is dropped and reported in `warnings`. The sample map
contains exactly one such stray: a boundary polygon named `land`, whose coordinates
carry an altitude of 30 m. Left in, it would punch a 240 m hole into a landscape that
otherwise runs from 267 m to 298 m.

**2 · Rebuild the surface.** Contours say *where* a height runs; hydrology needs a
value *everywhere*. Every contour vertex becomes a control point, and a Delaunay
triangulation interpolates linearly between them — the standard TIN approach, which
reproduces the original elevations exactly along the contours. Cells outside the
surveyed area fall back to nearest-neighbour, and the share of such cells is reported
as `extrapolated_fraction` so you know how much of the surface is inferred.

Rows and columns follow the ground's aspect ratio, so a cell is square in metres.
The D8 flow model assumes that.

**2b · Or download the surface.** Asked for an area instead of a file, the same grid
is filled from OpenZenith's terrain tiles. Each tile is a 256×256 raster of int16
metres on the web-mercator pyramid, so one request carries 65,536 elevations and a
whole study area is a handful of requests rather than tens of thousands of point
lookups. The zoom is chosen as the coarsest whose pixels still resolve one grid cell
— sampling finer than the grid only wastes requests — and the tiles are mosaicked and
bilinearly resampled onto exactly the grid `grid.py` would have built for the same
extent. Tiles are immutable, so they are cached on disk; re-running over the same
ground costs nothing.

Downloaded elevations are whole metres, which would leave the surface terraced into
flats that flow routing cannot cross, so the same light 3×3 average that removes
triangulation seams from a contour grid is applied here too.

**3 · Route the water.** Pits are filled by priority flood (Wang & Liu, 2006), tilted
by a fraction of a millimetre per cell so water can cross the flats they leave behind.
Without this, every hollow strands its water and flow accumulation means nothing.
Each cell then drains to its steepest downhill neighbour of eight (O'Callaghan & Mark,
1984), and visiting cells from highest to lowest totals up how many cells drain
through each one.

The *unfilled* depths are kept separately: how much fill a cell needed is exactly how
deep a hollow sits there, which is what makes a site worth ponding.

**4 · Pick the site.** Every cell is scored on four things a contour map can answer:

| Criterion | Weight | Why |
|---|---|---|
| Depression depth | 0.30 | An existing hollow is storage you do not have to dig |
| Upstream catchment | 0.30 | No inflow, no pond |
| Slope | 0.25 | Flat ground means cheaper excavation and embankment |
| Relative elevation | 0.15 | Low ground collects runoff by gravity |

Each is normalised against the range present in *this* terrain, so the scores adapt to
whatever map arrives. Sites are then taken best-first while keeping them spaced apart,
so the result is a set of genuine alternatives rather than a cluster of neighbours.

**You cannot store water in a river.** Before anything is scored, the map is split by
how much land drains through each cell — in hectares, which means the same thing on
every map:

| Upstream catchment | What it is | What happens |
|---|---|---|
| under 25 ha | Farm pond ground | Ranked as a pond site |
| 25 – 100 ha | Nala bund or percolation tank | Ranked separately, as that structure |
| over 100 ha, plus 40 m either side | River channel | No structure proposed; drawn on the map as excluded |

The thresholds come from how these structures are actually sized: the ICAR dryland
manuals and the MGNREGA works schedule put a farm pond's catchment at roughly 1–10 ha,
and past about 25 ha the water arriving in a storm has to be passed on rather than
held, which is a spillway problem. Standing water already on the ground — flat, level
to the limit of what the source can express, and larger than any farm pond — is
excluded as well.

**Size is only half of it — the other half is height.** A pond can sit two hundred
metres from the channel and still be in the river, because what floods is the valley
floor, and a valley floor is as wide as it is rather than as wide as a buffer. So every
cell also carries its **height above nearest drainage** (HAND: follow the flow pointers
downstream, find the first channel they reach, take the elevation difference). Ground
standing less than 3 m above the river it drains into, or less than 1 m above its nala,
is that channel's floodplain and is withheld.

Distance is the wrong question and height is the right one: ground fifty metres from a
river but eight metres above it is dry, and ground three hundred metres away but level
with it is riverbed every monsoon. On the sample survey the flow model found 140 cells
of channel — while the flat ground lying within a metre of its level was a fifth of the
map, and *every* site the scoring picked was inside it. Nothing was on the blue line;
everything was at the blue line's elevation. The exclusion is drawn on the map as a
wash, because a river drawn as a line is a line, and the ground it floods is an area.

The height test is never applied to the channels themselves: a nala is level with the
river it runs into by construction, so measuring one against the other would delete
every drainage line on a gentle gradient — and with it the bunds that are the whole
alternative on offer.

**Why hectares, and not a hollow's depth.** A wide river reads as a *deep* trench in an
elevation model: the sensor returns its water surface rather than its bed, and noise,
bridges and bank canopy all cut into it. So a rule of "busy but shallow" exempts
precisely the rivers it exists to catch, and the deeper and more river-like the
reading, the more certain the exemption. Nor can it be a percentile: a percentile calls
a fixed share of *every* map a channel, so a map lying wholly inside one river basin
still nominates the river bed, while a hillside with no stream on it still loses its
busiest ground.

The depth rule is still there, one layer down, for the small channels the size rule
lets through: a gully carrying a lot of flow with no hollow deeper than the source
could record is interpolation over a channel, not storage. The bar is the terrain's own
**vertical resolution** — the contour interval of a survey, or one metre for the
integer-metre DEM tiles — because below that the map does not know whether anything is
there.

**Nothing is dropped silently.** A place that is not a pond is still a place: nala-class
sites come back under `channel_structures`, labelled as the bund or percolation tank
they take, with the waste weir and the downstream consent that implies. The excluded
river is drawn on the map. And a hollow that one ordinary storm would fill and overtop
— 50 mm by the same rational method used for yield — is moved into that list too,
rather than being ranked as the best farm pond on the map with a note saying it is a
nala bund.

**What this cannot tell you** is whether a channel runs all year. A perennial river and
a monsoon nala are the same trench in an elevation model; only the flow regime separates
them, and that is not in the data. The classes are cut by size, which is conservative
and checkable. Knowing a river *by name* would need hydrography — OpenStreetMap
waterways, HydroSHEDS, India-WRIS — which this does not use.

**5 · Delineate the catchment.** Walking the flow pointers *upstream* from the chosen
site collects every cell that drains to it. Area is the cell count times the cell area;
the boundary is those cells merged into one polygon. The longest single drainage path
gives the time of concentration by Kirpich's formula, which is what sizes a spillway.

**6 · Estimate the water.** Filling adds water to the site's hollow until it reaches
the rim, giving surface area and volume that cannot run away across the map. Yield
comes from the rational method, `V = P × A × C`, reported per millimetre of rainfall
so it stays useful even when no rainfall figure is supplied.

### What this is not

Planning estimates from terrain geometry, not an engineering design. There is no soil,
land use, groundwater or existing-infrastructure data here; the contour interval sets
the vertical resolution, and anything shallower than it is invisible. Confirm a site
on the ground before committing to it.

Downloaded terrain deserves more caution than a survey, and the response says so in
`warnings` on every area analysis. The source is a **surface** model at about 30 m:
tree canopy and rooftops sit in it as if they were ground, which can invent a hollow
that is really a clearing or hide one under a windbreak. It is good for finding ground
worth walking to. It is not good for deciding how much to dig.

---

## Layout

```
backend/
  main.py            FastAPI app: CORS, routes, static files
  config.py          Settings, all with defaults — no API keys needed
  models.py          The response schema
  api/routes.py      HTTP layer: validate the upload, map errors to status codes
  core/
    kml.py           KML/KMZ → contour lines
    grid.py          Contour lines → elevation grid
    elevation_api.py Coordinates → elevation grid, via the OpenZenith service
    hydrology.py     Sink filling, D8 flow, accumulation, catchment delineation
    siting.py        Scoring and ranking pond sites
    render.py        Elevation and hillshade PNG overlays
    geo.py           Distances and areas on the ellipsoid
    pipeline.py      Sequences the stages and assembles the response
  tests/            106 tests over the parser, the terrain maths, the DEM client and the API
frontend/src/
  App.tsx            Layout, theme, and the location the map is pointing at
  api.ts             The API calls
  theme.ts           Light/dark, remembered, following the system by default
  geo.ts             The one bit of geodesy the browser needs: the area box
  components/        Terrain form (file or location), report panel, map
contours_1m.kml      Sample contour map
```

Each stage is a plain function over plain data, so a stage can be swapped without
touching the others. Both terrain sources hand `pipeline._report` the same
`ElevationGrid` and nothing downstream knows the difference — which is why adding the
elevation service meant one new module and one new entry point, not a rewrite.
Changing what makes a good pond site still means editing the weights in `siting.py`.

## Configuration

Everything has a working default and nothing needs an API key. The settings worth
knowing about, all read from the environment:

| Variable | Default | What it does |
|---|---|---|
| `ELEVATION_API_URL` | `https://openzenith.cyopsys.com` | Where terrain is downloaded from |
| `ELEVATION_API_ENABLED` | `1` | Set to `0` to refuse area analysis and serve uploads only |
| `MAX_AREA_KM` | `12` | Largest area, per side, that may be requested |
| `ELEVATION_MAX_TILES` | `64` | Tiles one analysis may download before it is refused |
| `ELEVATION_CACHE_DIR` | `.dem_cache/` | Where downloaded tiles are kept |
| `GRID_RESOLUTION` | `160` | Default grid cells along the longer side |
| `STORAGE_DIR` | `storage/` | Where overlay PNGs are written |

## Tests

```bash
.venv/bin/python -m pytest
```

No test needs the network. The suite runs against two contour maps — the sample, and
a synthetic hill generated in a different hemisphere at a different scale with a
different contour interval — and against a stand-in elevation service that answers
tile requests with a hill computed from each pixel's real latitude and longitude.

Both of those are the same idea. The synthetic map proves the answers come from the
uploaded file rather than from anything tuned to the sample; the synthetic hill proves
the downloaded surface lands where the projection maths says it should, which is the
part of a tile client most likely to be quietly wrong.

One test does call the live service, and is skipped unless you ask for it:

```bash
ELEVATION_LIVE_TESTS=1 .venv/bin/python -m pytest -k live
```

It checks the service against `contours_1m.kml`: the surveyed contours say that
hillside runs from 267 m to 298 m, and the download for the same footprint has to
agree.
# catchment_analyzer
