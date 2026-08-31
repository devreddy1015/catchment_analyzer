import { useEffect, useRef, useState } from 'react';
import { Crosshair, FileUp, Loader2, Play, Search } from 'lucide-react';
import { lookupElevation, searchPlaces } from '../api';
import type { AnalysisRequest, ElevationPoint, MapLocation, Place } from '../types';

interface Props {
  busy: boolean;
  mode: 'file' | 'area';
  location: MapLocation;
  onModeChange: (mode: 'file' | 'area') => void;
  onLocationChange: (location: MapLocation) => void;
  onRun: (request: AnalysisRequest) => void;
}

const ACCEPTED = '.kml,.kmz';

type Field = 'lat' | 'lon' | 'area';

/** What each editable number is allowed to be, and where it lives on a location. */
const LIMITS: Record<Field, { min: number; max: number }> = {
  lat: { min: -89, max: 89 },
  lon: { min: -180, max: 180 },
  area: { min: 0.25, max: 12 },
};

/** Long enough to stop typing before a search goes out. */
const SEARCH_DELAY_MS = 350;

/** Where the terrain comes from — an uploaded survey or a place on the map —
 *  plus the four knobs both ways in share. */
export default function TerrainCard({
  busy, mode, location, onModeChange, onLocationChange, onRun,
}: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [hot, setHot] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<Place[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Coordinate boxes hold text while they are being typed. Committing
  // `Number(value)` on every keystroke turns a cleared box into 0, which is a
  // real place — the Atlantic — and the pin would silently go there.
  const [draft, setDraft] = useState<Record<Field, string | null>>({ lat: null, lon: null, area: null });
  const typing = useRef(false);

  const [probe, setProbe] = useState<ElevationPoint | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);

  const [resolution, setResolution] = useState(160);
  const [maxSites, setMaxSites] = useState(5);
  const [coefficient, setCoefficient] = useState(0.4);
  const [rainfall, setRainfall] = useState('');

  // A new pin means the old elevation reading is about somewhere else. Any
  // half-typed coordinate is stale too — but only if the pin moved because of a
  // search or a map click. Discarding it on our own keystroke would eat the dot
  // out of "21." the moment it parsed as 21.
  useEffect(() => {
    setProbe(null);
    setProbeError(null);
    if (typing.current) { typing.current = false; return; }
    setDraft({ lat: null, lon: null, area: null });
  }, [location.latitude, location.longitude]);

  // Search as you type, once you have stopped.
  useEffect(() => {
    const term = query.trim();
    if (term.length < 2) {
      setMatches(null);
      setSearchError(null);
      return;
    }
    let live = true;
    setSearching(true);
    const timer = setTimeout(async () => {
      try {
        const found = await searchPlaces(term);
        if (live) { setMatches(found); setSearchError(null); }
      } catch {
        if (live) { setMatches(null); setSearchError('Place search is unavailable.'); }
      } finally {
        if (live) setSearching(false);
      }
    }, SEARCH_DELAY_MS);

    return () => { live = false; clearTimeout(timer); };
  }, [query]);

  const accept = (chosen: File | undefined) => {
    if (chosen && /\.(kml|kmz)$/i.test(chosen.name)) setFile(chosen);
  };

  const shown: Record<Field, string> = {
    lat: draft.lat ?? String(location.latitude),
    lon: draft.lon ?? String(location.longitude),
    area: draft.area ?? String(location.areaKm),
  };

  const valid = (field: Field, text: string): boolean => {
    const value = Number(text);
    const { min, max } = LIMITS[field];
    return text.trim() !== '' && Number.isFinite(value) && value >= min && value <= max;
  };

  /** Keep the text, and move the pin only once the text is a usable number. */
  const edit = (field: Field, text: string) => {
    setDraft({ ...draft, [field]: text });
    if (!valid(field, text)) return;
    const value = Number(text);
    typing.current = true;
    onLocationChange({
      ...location,
      ...(field === 'lat' ? { latitude: value, label: null } : {}),
      ...(field === 'lon' ? { longitude: value, label: null } : {}),
      ...(field === 'area' ? { areaKm: value } : {}),
    });
  };

  /** On leaving a box, show what the pin is actually on rather than a half-edit. */
  const settle = () => setDraft({ lat: null, lon: null, area: null });

  const choose = (place: Place) => {
    settle();
    onLocationChange({
      latitude: Number(place.latitude.toFixed(5)),
      longitude: Number(place.longitude.toFixed(5)),
      areaKm: location.areaKm,
      label: place.name,
    });
    setQuery('');
    setMatches(null);
  };

  /** One cheap request that says whether the pin is on land with data. */
  const check = async () => {
    setProbing(true);
    setProbe(null);
    setProbeError(null);
    try {
      setProbe(await lookupElevation(location.latitude, location.longitude));
    } catch {
      setProbeError('No data at the pin.');
    } finally {
      setProbing(false);
    }
  };

  const settings = {
    resolution,
    maxSites,
    runoffCoefficient: coefficient,
    rainfallMm: rainfall.trim() === '' ? null : Number(rainfall),
  };

  const ready = mode === 'file' ? file !== null : true;

  const submit = () => {
    if (mode === 'file' && file) onRun({ kind: 'file', file, ...settings });
    if (mode === 'area') {
      onRun({
        kind: 'area',
        latitude: location.latitude,
        longitude: location.longitude,
        areaKm: location.areaKm,
        ...settings,
      });
    }
  };

  return (
    <section className="card">
      <header>
        <h2>Terrain</h2>
        <span className="eyebrow">step one</span>
      </header>

      <div className="segmented modes">
        <button className={mode === 'file' ? 'on' : ''} onClick={() => onModeChange('file')}>
          Contour file
        </button>
        <button className={mode === 'area' ? 'on' : ''} onClick={() => onModeChange('area')}>
          Map location
        </button>
      </div>

      {mode === 'file' ? (
        <>
          <button
            type="button"
            className={`dropzone${hot ? ' hot' : ''}${file ? ' loaded' : ''}`}
            onClick={() => input.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setHot(true); }}
            onDragLeave={() => setHot(false)}
            onDrop={(e) => { e.preventDefault(); setHot(false); accept(e.dataTransfer.files[0]); }}
          >
            <FileUp size={20} strokeWidth={1.5} />
            <strong>{file ? file.name : 'Drop a KML or KMZ file'}</strong>
            <small>{file ? `${(file.size / 1e6).toFixed(1)} MB — click to replace` : 'or click to browse'}</small>
          </button>
          <input
            ref={input}
            type="file"
            accept={ACCEPTED}
            hidden
            onChange={(e) => accept(e.target.files?.[0])}
          />
        </>
      ) : (
        <>
          <div className="search">
            <div className="search-box">
              {searching ? <Loader2 size={15} className="spin" /> : <Search size={15} strokeWidth={1.7} />}
              <input
                type="search"
                value={query}
                placeholder="Search a place — Durg, Bhilai, Nashik…"
                aria-label="Search for a place"
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            {matches && (
              <ul className="results">
                {matches.length === 0 ? (
                  <li className="empty">Nothing found for “{query.trim()}”.</li>
                ) : (
                  matches.map((place) => (
                    <li key={`${place.latitude},${place.longitude},${place.name}`}>
                      <button type="button" onClick={() => choose(place)}>
                        <span className="place">{place.name}</span>
                        <span className="kind">{place.kind}</span>
                      </button>
                    </li>
                  ))
                )}
              </ul>
            )}
            {searchError && <p className="footnote">{searchError} Type coordinates below instead.</p>}
          </div>

          <p className="pinned">
            <strong>{location.label ?? 'Dropped pin'}</strong>
            <span>Click the map to move it.</span>
          </p>

          <div className="fields">
            <div className="field">
              <label htmlFor="lat">Latitude</label>
              <input
                id="lat"
                type="text"
                inputMode="decimal"
                value={shown.lat}
                aria-invalid={!valid('lat', shown.lat)}
                onChange={(e) => edit('lat', e.target.value)}
                onBlur={settle}
              />
              <span className="hint">degrees N</span>
            </div>
            <div className="field">
              <label htmlFor="lon">Longitude</label>
              <input
                id="lon"
                type="text"
                inputMode="decimal"
                value={shown.lon}
                aria-invalid={!valid('lon', shown.lon)}
                onChange={(e) => edit('lon', e.target.value)}
                onBlur={settle}
              />
              <span className="hint">degrees E</span>
            </div>
            <div className="field">
              <label htmlFor="area">Area</label>
              <input
                id="area"
                type="text"
                inputMode="decimal"
                value={shown.area}
                aria-invalid={!valid('area', shown.area)}
                onChange={(e) => edit('area', e.target.value)}
                onBlur={settle}
              />
              <span className="hint">km across</span>
            </div>
            <div className="field">
              <label>Ground</label>
              <button type="button" className="probe" onClick={check} disabled={probing}>
                {probing ? <Loader2 size={13} className="spin" /> : <Crosshair size={13} />}
                {probe ? `${probe.elevation_m.toFixed(0)} m` : 'check'}
              </button>
              <span className="hint">{probeError ?? probe?.surface_type ?? 'elevation here'}</span>
            </div>
          </div>

          <p className="footnote">
            Elevations come from <a href="https://openzenith.org" target="_blank" rel="noreferrer">OpenZenith</a>,
            sampled at about 30 m. Good enough to find ground worth walking; not a survey.
          </p>
        </>
      )}

      <div className="fields">
        <div className="field">
          <label htmlFor="resolution">Grid detail</label>
          <input
            id="resolution"
            type="number"
            min={48}
            max={260}
            step={10}
            value={resolution}
            onChange={(e) => setResolution(Number(e.target.value))}
          />
          <span className="hint">cells across</span>
        </div>
        <div className="field">
          <label htmlFor="sites">Candidates</label>
          <input
            id="sites"
            type="number"
            min={1}
            max={20}
            value={maxSites}
            onChange={(e) => setMaxSites(Number(e.target.value))}
          />
          <span className="hint">sites to rank</span>
        </div>
        <div className="field">
          <label htmlFor="coefficient">Runoff C</label>
          <select id="coefficient" value={coefficient} onChange={(e) => setCoefficient(Number(e.target.value))}>
            <option value={0.15}>0.15 — forest</option>
            <option value={0.30}>0.30 — pasture</option>
            <option value={0.40}>0.40 — farmland</option>
            <option value={0.60}>0.60 — bare soil</option>
            <option value={0.80}>0.80 — built up</option>
          </select>
          <span className="hint">land cover</span>
        </div>
        <div className="field">
          <label htmlFor="rainfall">Rainfall</label>
          <input
            id="rainfall"
            type="number"
            min={0}
            placeholder="optional"
            value={rainfall}
            onChange={(e) => setRainfall(e.target.value)}
          />
          <span className="hint">mm per year</span>
        </div>
      </div>

      <button className="run" onClick={submit} disabled={!ready || busy}>
        {busy ? <Loader2 size={15} className="spin" /> : <Play size={15} />}
        {busy ? 'Analysing…' : 'Find the catchment'}
      </button>
    </section>
  );
}
