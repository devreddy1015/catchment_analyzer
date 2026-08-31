import { useEffect, useState } from 'react';
import { Droplets, Moon, PlugZap, Sun, XCircle } from 'lucide-react';
import TerrainCard from './components/TerrainCard';
import ReportPanel from './components/ReportPanel';
import CatchmentMap from './components/CatchmentMap';
import { analyze, apiIsUp, errorMessage } from './api';
import { applyTheme, resolve, storedTheme, watchSystem, type Theme } from './theme';
import type { Analysis, AnalysisRequest, MapLocation, Site } from './types';

const STEPS = [
  'read the terrain',
  'rebuild the surface',
  'route water downhill',
  'rank pond sites',
  'delineate the catchment',
];

/** How often to look for a backend that was not there a moment ago. */
const HEALTH_POLL_MS = 3000;

/** Where the map looks before anyone has chosen anywhere. */
const START: MapLocation = {
  latitude: 21.2517,
  longitude: 81.297,
  areaKm: 3,
  label: 'Durg district, Chhattisgarh',
};

export default function App() {
  const [result, setResult] = useState<Analysis | null>(null);
  const [selected, setSelected] = useState<Site | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Lifted out of the form so the map can draw the area before it is analysed.
  const [mode, setMode] = useState<'file' | 'area'>('area');
  const [location, setLocation] = useState<MapLocation>(START);

  // Checked on load rather than discovered by a failed analysis: forgetting the
  // backend is the easiest mistake here, and it is worth saying so up front.
  const [offline, setOffline] = useState(false);
  useEffect(() => { apiIsUp().then((up) => setOffline(!up)); }, []);

  // And checked again while it is down. Telling someone to start the backend and
  // then still accusing them after they have is worse than not telling them: the
  // page has to notice on its own, without a reload nobody thought to do.
  useEffect(() => {
    if (!offline) return;
    const recheck = () => apiIsUp().then((up) => setOffline(!up));
    const timer = window.setInterval(recheck, HEALTH_POLL_MS);
    window.addEventListener('focus', recheck);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', recheck);
    };
  }, [offline]);

  const [theme, setTheme] = useState<Theme>(storedTheme);

  // The resolved theme is state, not a derived value: the map picks its basemap
  // from it, and under "system" it can change without anything in React changing.
  // Derived, it would go stale and leave a light map on a dark page.
  const [painted, setPainted] = useState(() => resolve(storedTheme()));

  useEffect(() => {
    applyTheme(theme);
    setPainted(resolve(theme));
    if (theme !== 'system') return;
    return watchSystem(() => {
      applyTheme('system');
      setPainted(resolve('system'));
    });
  }, [theme]);

  const run = async (request: AnalysisRequest) => {
    setBusy(true);
    setError(null);
    try {
      const analysis = await analyze(request);
      setResult(analysis);
      setSelected(analysis.pond_site);
    } catch (err) {
      setError(errorMessage(err));
      setResult(null);
      setSelected(null);
      apiIsUp().then((up) => setOffline(!up));  // Was it this request, or the whole API?
    } finally {
      setBusy(false);
    }
  };

  /** Moving the pin invalidates the result that was drawn for the old one. */
  const moveTo = (next: MapLocation) => {
    setLocation(next);
    setResult(null);
    setSelected(null);
    setError(null);
  };

  return (
    <div className="shell">
      <header className="masthead">
        <Droplets size={19} strokeWidth={1.6} color="var(--water)" />
        <h1>Catchment Analyzer</h1>
        <span className="rule" />
        <span className="tag">terrain → pond site → catchment</span>
        <button
          className="theme-toggle"
          onClick={() => setTheme(painted === 'dark' ? 'light' : 'dark')}
          title={`Switch to ${painted === 'dark' ? 'light' : 'dark'} mode`}
          aria-label={`Switch to ${painted === 'dark' ? 'light' : 'dark'} mode`}
        >
          {painted === 'dark' ? <Sun size={15} strokeWidth={1.7} /> : <Moon size={15} strokeWidth={1.7} />}
        </button>
      </header>

      <div className="workspace">
        <div className="column">
          {offline && (
            <div className="notice error">
              <PlugZap size={15} style={{ flexShrink: 0, marginTop: 2 }} />
              <span>
                The API is not running. Start it from the project root with{' '}
                <code>.venv/bin/uvicorn backend.main:app --reload --port 8000</code>, or run{' '}
                <code>./dev.sh</code> to start the API and this page together. This notice
                clears itself once the API answers — no need to reload.
              </span>
            </div>
          )}
          <TerrainCard
            busy={busy}
            mode={mode}
            location={location}
            onModeChange={setMode}
            onLocationChange={moveTo}
            onRun={run}
          />
          {error && (
            <div className="notice error">
              <XCircle size={15} style={{ flexShrink: 0, marginTop: 2 }} />
              <span>{error}</span>
            </div>
          )}
          {result && selected && (
            <ReportPanel result={result} selected={selected} onSelect={setSelected} />
          )}
        </div>

        <div className="stage">
          <CatchmentMap
            result={result}
            selected={selected}
            location={location}
            mode={mode}
            theme={painted}
            onSelect={setSelected}
            onPick={(latitude, longitude) => moveTo({ ...location, latitude, longitude, label: null })}
          />

          {!result && !busy && (
            <div className="map-panel hint">
              <span className="eyebrow">No analysis yet</span>
              <p>
                {mode === 'area'
                  ? 'Search for a place or click the map to move the pin, then find the catchment.'
                  : 'Drop a contour map on the left to see the terrain it describes.'}
              </p>
              <ol className="steps">
                {STEPS.map((step, i) => <li key={step}>{i + 1}. {step}</li>)}
              </ol>
            </div>
          )}

          {busy && (
            <div className="curtain">
              <div className="spinner" />
              <h2>Analysing the terrain</h2>
              <p>Reading elevations, rebuilding the surface and tracing where water goes.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
