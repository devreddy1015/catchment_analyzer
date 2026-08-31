// Mirrors the responses of POST /api/analyzeContour and POST /api/analyzeArea.

export interface Bounds { south: number; west: number; north: number; east: number }
export interface Point { latitude: number; longitude: number }

/** Where the terrain came from. `kind` says which of the two it is. */
interface SourceBase {
  name: string;
  format: string;
  elevation_min_m: number;
  elevation_max_m: number;
  bounds: Bounds;
}

/** An uploaded contour survey. */
export interface ContourSource extends SourceBase {
  kind: 'contour_file';
  contour_lines: number;
  vertices: number;
  elevation_levels: number;
  contour_interval_m: number;
}

/** Elevations downloaded for an area. */
export interface ServiceSource extends SourceBase {
  kind: 'elevation_service';
  provider: string;
  sample_spacing_m: number;
  tiles_fetched: number;
  tile_zoom: number;
}

export type Source = ContourSource | ServiceSource;

export interface Grid {
  rows: number;
  cols: number;
  cell_size_m: number;
  cell_area_m2: number;
  extrapolated_fraction: number;
}

export interface Terrain {
  min_elevation_m: number;
  max_elevation_m: number;
  mean_elevation_m: number;
  relief_m: number;
  mean_slope_deg: number;
  max_slope_deg: number;
  mapped_area_km2: number;
  bounds: Bounds;
  grid: Grid;
}

export interface Storage {
  depth_m: number;
  spill_elevation_m: number;
  surface_area_m2: number;
  volume_m3: number;
}

export interface Site {
  rank: number;
  latitude: number;
  longitude: number;
  elevation_m: number;
  slope_deg: number;
  depression_depth_m: number;
  upstream_cells: number;
  score: number;
  rating: string;
  score_breakdown: Record<string, number>;
  storage: Storage;
  reasons: string[];
}

export interface Runoff {
  method: string;
  runoff_coefficient: number;
  yield_m3_per_mm: number;
  rainfall_mm: number | null;
  runoff_m3: number | null;
}

export interface Catchment {
  outlet: Point;
  area_m2: number;
  area_km2: number;
  area_hectares: number;
  cell_count: number;
  perimeter_m: number;
  min_elevation_m: number;
  max_elevation_m: number;
  relief_m: number;
  mean_slope_deg: number;
  max_slope_deg: number;
  longest_flow_path_m: number;
  average_gradient: number;
  time_of_concentration_min: number;
  share_of_map: number;
  runoff: Runoff;
}

export interface Overlays { elevation: string; hillshade: string; bounds: Bounds }

export interface Options {
  resolution: number;
  max_sites: number;
  runoff_coefficient: number;
  rainfall_mm: number | null;
  centre: Point | null;
  area_km: number | null;
}

export interface GeoFeature {
  type: 'Feature';
  geometry: { type: string; coordinates: any };
  properties: Record<string, any>;
}

export interface Analysis {
  success: boolean;
  analysis_id: string;
  generated_at: string;
  options: Options;
  source: Source;
  terrain: Terrain;
  pond_site: Site;
  catchment: Catchment;
  alternative_sites: Site[];
  overlays: Overlays;
  geojson: { type: 'FeatureCollection'; features: GeoFeature[] };
  warnings: string[];
}

/** The knobs both ways in share. */
export interface AnalysisSettings {
  resolution: number;
  maxSites: number;
  runoffCoefficient: number;
  rainfallMm: number | null;
}

/** Analyse an uploaded contour survey. */
export interface FileRequest extends AnalysisSettings {
  kind: 'file';
  file: File;
}

/** Analyse a place on the map, terrain downloaded from the elevation service. */
export interface AreaRequest extends AnalysisSettings {
  kind: 'area';
  latitude: number;
  longitude: number;
  areaKm: number;
}

export type AnalysisRequest = FileRequest | AreaRequest;

/** One result from GET /api/places. */
export interface Place {
  name: string;
  latitude: number;
  longitude: number;
  kind: string | null;
}

/** The area the map is pointing at, before anything has been analysed. */
export interface MapLocation {
  latitude: number;
  longitude: number;
  areaKm: number;
  label: string | null;
}

/** One reading from GET /api/elevation. */
export interface ElevationPoint {
  latitude: number;
  longitude: number;
  elevation_m: number;
  surface_type: string;
  resolution_m: number | null;
  source: string;
}
