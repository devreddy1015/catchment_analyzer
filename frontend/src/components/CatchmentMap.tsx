import { useEffect, useMemo, useState } from 'react';
import {
  ImageOverlay, MapContainer, Marker, Polygon, Polyline, Popup, Rectangle, TileLayer,
  useMap, useMapEvents,
} from 'react-leaflet';
import L, { LatLngBoundsExpression } from 'leaflet';
import type { Analysis, Bounds, GeoFeature, MapLocation, Site } from '../types';
import { bboxAround } from '../geo';
import { area, coords, num, volume } from '../format';

interface Props {
  result: Analysis | null;
  selected: Site | null;
  location: MapLocation;
  mode: 'file' | 'area';
  theme: 'light' | 'dark';
  onSelect: (site: Site) => void;
  onPick: (latitude: number, longitude: number) => void;
}

type Basemap = 'streets' | 'satellite' | 'plain';

const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';

/** The plain basemap follows the interface, so a dark page is not lit up by its map. */
const BASEMAPS = (theme: 'light' | 'dark'): Record<Basemap, { url: string; attribution: string }> => ({
  streets: {
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: '&copy; OpenStreetMap',
  },
  satellite: {
    url: `${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`,
    attribution: 'Imagery &copy; Esri',
  },
  plain: {
    url: `${ESRI}/Canvas/World_${theme === 'dark' ? 'Dark' : 'Light'}_Gray_Base/MapServer/tile/{z}/{y}/{x}`,
    attribution: '&copy; Esri, HERE, Garmin, &copy; OpenStreetMap',
  },
});

const box = (b: Bounds): LatLngBoundsExpression => [[b.south, b.west], [b.north, b.east]];

/** GeoJSON is [lon, lat]; Leaflet wants [lat, lon]. */
const flip = (ring: number[][]): [number, number][] => ring.map(([x, y]) => [y, x]);

const pin = (rank: number, primary: boolean) =>
  L.divIcon({
    className: '',
    html: `<div class="pin ${primary ? 'primary' : 'other'}"><b>${rank}</b></div>`,
    iconSize: [26, 26],
    iconAnchor: [13, 24],
  });

/** The pin for a place chosen but not yet analysed. */
const target = L.divIcon({
  className: '',
  html: '<div class="pin target"><b>+</b></div>',
  iconSize: [26, 26],
  iconAnchor: [13, 24],
});

/** Re-frame the map whenever what it is showing changes. */
function FitTo({ bounds, token }: { bounds: Bounds; token: string }) {
  const map = useMap();
  useEffect(() => {
    map.fitBounds(box(bounds), { padding: [26, 26] });
  }, [map, token]);
  return null;
}

/** Clicking the map moves the pin — the shortest path from "there" to coordinates. */
function ClickToPlace({ onPick }: { onPick: (lat: number, lon: number) => void }) {
  useMapEvents({ click: (event) => onPick(event.latlng.lat, event.latlng.lng) });
  return null;
}

function feature(result: Analysis, kind: string): GeoFeature | undefined {
  return result.geojson.features.find((f) => f.properties.type === kind);
}

export default function CatchmentMap({
  result, selected, location, mode, theme, onSelect, onPick,
}: Props) {
  const [basemap, setBasemap] = useState<Basemap>('plain');
  const [showElevation, setShowElevation] = useState(true);
  const [showHillshade, setShowHillshade] = useState(true);
  const [showCatchment, setShowCatchment] = useState(true);
  const [showDrainage, setShowDrainage] = useState(true);
  const [showFlowPath, setShowFlowPath] = useState(true);
  const [opacity, setOpacity] = useState(0.6);

  const sites = useMemo(
    () => (result ? [result.pond_site, ...result.alternative_sites] : []),
    [result],
  );
  const catchmentRing = useMemo(() => {
    const f = result && feature(result, 'catchment');
    return f ? flip(f.geometry.coordinates[0]) : [];
  }, [result]);
  const flowPath = useMemo(() => {
    const f = result && feature(result, 'longest_flow_path');
    return f ? flip(f.geometry.coordinates) : [];
  }, [result]);
  const drainage = useMemo(() => {
    const f = result && feature(result, 'drainage_lines');
    return f ? (f.geometry.coordinates as number[][][]).map(flip) : [];
  }, [result]);

  // What the map frames: the analysis if there is one, otherwise the area a run
  // would cover. `token` is what tells FitTo that this is a new thing to look at.
  const preview = useMemo(
    () => bboxAround(location.latitude, location.longitude, location.areaKm),
    [location.latitude, location.longitude, location.areaKm],
  );
  const framing = result ? result.terrain.bounds : preview;
  const token = result
    ? result.analysis_id
    : `${location.latitude},${location.longitude},${location.areaKm}`;

  const showPreview = !result && mode === 'area';

  return (
    <>
      <MapContainer
        center={[location.latitude, location.longitude]}
        zoom={13}
        zoomControl
        style={{ height: '100%' }}
      >
        <TileLayer key={`${basemap}-${theme}`} {...BASEMAPS(theme)[basemap]} />
        <FitTo bounds={framing} token={token} />
        {mode === 'area' && <ClickToPlace onPick={onPick} />}

        {showPreview && (
          <>
            <Rectangle
              bounds={box(preview)}
              pathOptions={{ color: '#146b6b', weight: 1.5, dashArray: '6 5', fillOpacity: 0.06 }}
            />
            <Marker position={[location.latitude, location.longitude]} icon={target}>
              <Popup>
                <strong>{location.label ?? 'Chosen location'}</strong><br />
                {coords(location.latitude, location.longitude)}<br />
                {location.areaKm} km across — click anywhere to move this
              </Popup>
            </Marker>
          </>
        )}

        {result && (
          <>
            {showElevation && (
              <ImageOverlay url={result.overlays.elevation} bounds={box(result.overlays.bounds)} opacity={opacity} />
            )}
            {showHillshade && (
              <ImageOverlay url={result.overlays.hillshade} bounds={box(result.overlays.bounds)} opacity={0.45} />
            )}

            {showDrainage && drainage.map((line, i) => (
              <Polyline key={i} positions={line} pathOptions={{ color: '#2f6fa8', weight: 1.1, opacity: 0.65 }} />
            ))}

            {showCatchment && catchmentRing.length > 0 && (
              <Polygon
                positions={catchmentRing}
                pathOptions={{ color: '#146b6b', weight: 2.5, fillColor: '#146b6b', fillOpacity: 0.14 }}
              >
                <Popup>
                  <strong>Catchment</strong><br />
                  {area(result.catchment.area_m2).value} {area(result.catchment.area_m2).unit} ·{' '}
                  {num(result.catchment.cell_count)} cells
                </Popup>
              </Polygon>
            )}

            {showFlowPath && flowPath.length > 1 && (
              <Polyline positions={flowPath} pathOptions={{ color: '#b2542c', weight: 2.5, dashArray: '5 4' }} />
            )}

            {sites.map((site) => (
              <Marker
                key={site.rank}
                position={[site.latitude, site.longitude]}
                icon={pin(site.rank, site.rank === selected?.rank)}
                eventHandlers={{ click: () => onSelect(site) }}
              >
                <Popup>
                  <strong>Site {site.rank} — {site.rating}</strong><br />
                  {coords(site.latitude, site.longitude)}<br />
                  Score {site.score} · {num(site.elevation_m, 1)} m · {num(site.slope_deg, 1)}°<br />
                  Holds {volume(site.storage.volume_m3)}
                </Popup>
              </Marker>
            ))}
          </>
        )}
      </MapContainer>

      <div className="map-panel layers">
        <span className="eyebrow">Layers</span>
        {result ? (
          ([
            ['Elevation tint', showElevation, setShowElevation],
            ['Hillshade', showHillshade, setShowHillshade],
            ['Catchment', showCatchment, setShowCatchment],
            ['Drainage lines', showDrainage, setShowDrainage],
            ['Longest flow path', showFlowPath, setShowFlowPath],
          ] as const).map(([label, value, set]) => (
            <label className="toggle" key={label}>
              <input type="checkbox" checked={value} onChange={(e) => set(e.target.checked)} />
              {label}
            </label>
          ))
        ) : (
          <p className="muted">Layers appear once there is something to draw.</p>
        )}
        {result && (
          <input
            className="range"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={opacity}
            onChange={(e) => setOpacity(Number(e.target.value))}
            aria-label="Elevation tint opacity"
          />
        )}
        <div className="segmented">
          {(['plain', 'streets', 'satellite'] as Basemap[]).map((name) => (
            <button key={name} className={basemap === name ? 'on' : ''} onClick={() => setBasemap(name)}>
              {name}
            </button>
          ))}
        </div>
      </div>

      {result && (
        <div className="map-panel legend">
          <span className="eyebrow">Legend</span>
          <div className="swatches">
            <span className="swatch"><i className="area" style={{ background: 'rgba(20,107,107,0.3)', border: '1.5px solid #146b6b' }} /> Catchment</span>
            <span className="swatch"><i style={{ background: '#b2542c' }} /> Longest flow path</span>
            <span className="swatch"><i style={{ background: '#2f6fa8' }} /> Drainage lines</span>
            <span className="swatch"><i style={{ background: '#b2542c', height: 11, width: 11, borderRadius: '50%' }} /> Pond sites</span>
          </div>
        </div>
      )}
    </>
  );
}
