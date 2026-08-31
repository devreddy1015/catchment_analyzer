import axios from 'axios';
import type { Analysis, AnalysisRequest, AreaRequest, ElevationPoint, FileRequest, Place } from './types';

/** Upload a contour map and get its pond site and catchment back. */
export async function analyzeContour(request: FileRequest): Promise<Analysis> {
  const form = new FormData();
  form.append('file', request.file);
  form.append('resolution', String(request.resolution));
  form.append('max_sites', String(request.maxSites));
  form.append('runoff_coefficient', String(request.runoffCoefficient));
  if (request.rainfallMm !== null) form.append('rainfall_mm', String(request.rainfallMm));

  const { data } = await axios.post<Analysis>('/api/analyzeContour', form);
  return data;
}

/** Analyse a place on the map instead, with terrain from the elevation service. */
export async function analyzeArea(request: AreaRequest): Promise<Analysis> {
  const { data } = await axios.post<Analysis>('/api/analyzeArea', {
    latitude: request.latitude,
    longitude: request.longitude,
    area_km: request.areaKm,
    resolution: request.resolution,
    max_sites: request.maxSites,
    runoff_coefficient: request.runoffCoefficient,
    rainfall_mm: request.rainfallMm,
  });
  return data;
}

/** Either way in, one call. */
export function analyze(request: AnalysisRequest): Promise<Analysis> {
  return request.kind === 'file' ? analyzeContour(request) : analyzeArea(request);
}

/**
 * Elevation at one point — a single cheap request, used to tell someone whether
 * the coordinates they typed are land with data before they commit to a run.
 */
export async function lookupElevation(lat: number, lon: number): Promise<ElevationPoint> {
  const { data } = await axios.get<ElevationPoint>('/api/elevation', { params: { lat, lon } });
  return data;
}

/** Find a place by name, so a location can be typed instead of looked up. */
export async function searchPlaces(query: string, limit = 5): Promise<Place[]> {
  const { data } = await axios.get<Place[]>('/api/places', { params: { q: query, limit } });
  return data;
}

const UNREACHABLE =
  'Cannot reach the API. Start the backend from the project root with ' +
  '`.venv/bin/uvicorn backend.main:app --reload --port 8000`, or run `./dev.sh` to start both.';

/** Is the backend there at all? Asked once on load, so the answer arrives before
 *  someone fills in a form and waits for it to fail. */
export async function apiIsUp(): Promise<boolean> {
  try {
    const { data } = await axios.get<{ status?: string }>('/api/health', { timeout: 4000 });
    return data.status === 'ok';
  } catch {
    return false;
  }
}

/** Turn an axios failure into the message the API actually sent.
 *
 *  The API always answers with a JSON body carrying `detail`. Anything that comes
 *  back without one did not come from the API — it came from a proxy that could
 *  not reach it — and axios's own "Request failed with status code 500" is the
 *  least useful thing that could be shown at that point. */
export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const body = error.response?.data;
    if (body && typeof body === 'object') {
      const detail = (body as Record<string, unknown>).detail ?? (body as Record<string, unknown>).error;
      if (typeof detail === 'string') return detail;
    }
    if (!error.response || error.code === 'ERR_NETWORK' || error.code === 'ECONNABORTED') return UNREACHABLE;
    if ([500, 502, 503, 504].includes(error.response.status)) return UNREACHABLE;
    return `The API answered ${error.response.status} with nothing to say about why.`;
  }
  return error instanceof Error ? error.message : 'Something went wrong.';
}
