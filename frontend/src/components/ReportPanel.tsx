import { AlertTriangle, Waves } from 'lucide-react';
import type { Analysis, Site } from '../types';
import { area, coords, distance, num, volume } from '../format';

interface Props {
  result: Analysis;
  selected: Site;
  onSelect: (site: Site) => void;
}

const Reading = ({ label, value, unit }: { label: string; value: string; unit?: string }) => (
  <div className="reading">
    <dt>{label}</dt>
    <dd>{value}{unit && <small>{unit}</small>}</dd>
  </div>
);

const Row = ({ label, value }: { label: string; value: string }) => (
  <div className="row">
    <dt>{label}</dt>
    <dd>{value}</dd>
  </div>
);

/** Everything the API returned, read top to bottom as a survey report. */
export default function ReportPanel({ result, selected, onSelect }: Props) {
  const {
    source, terrain, catchment, pond_site, alternative_sites, warnings,
    watercourses, channel_structures,
  } = result;
  const surveyed = source.kind === 'contour_file';
  const catchmentArea = area(catchment.area_m2);
  const sites = [pond_site, ...alternative_sites];
  const runoff = catchment.runoff;

  return (
    <>
      {warnings.map((text) => (
        <div className="notice warn" key={text}>
          <AlertTriangle size={15} style={{ flexShrink: 0, marginTop: 2 }} />
          <span>{text}</span>
        </div>
      ))}

      <section className="card">
        <header>
          <h2>Catchment</h2>
          <span className="eyebrow">draining to the pond</span>
        </header>
        <div className="headline">
          <div className="value">{catchmentArea.value}<span>{catchmentArea.unit}</span></div>
          <p className="caption">
            {num(catchment.share_of_map * 100)}% of the mapped area drains to this point,
            across {num(catchment.cell_count)} grid cells.
          </p>
        </div>
        <dl className="readings" style={{ margin: 0 }}>
          <Reading label="Perimeter" value={distance(catchment.perimeter_m)} />
          <Reading label="Longest flow path" value={distance(catchment.longest_flow_path_m)} />
          <Reading label="Relief" value={num(catchment.relief_m, 1)} unit="m" />
          <Reading label="Mean slope" value={num(catchment.mean_slope_deg, 1)} unit="°" />
          <Reading label="Gradient" value={`1 in ${num(1 / Math.max(catchment.average_gradient, 1e-9))}`} />
          <Reading label="Time of concentration" value={num(catchment.time_of_concentration_min)} unit="min" />
        </dl>
      </section>

      <section className="card">
        <header>
          <h2>Water yield</h2>
          <span className="eyebrow">C = {runoff.runoff_coefficient}</span>
        </header>
        <dl className="rows">
          <Row label="Per mm of rain" value={`${num(runoff.yield_m3_per_mm)} m³`} />
          {runoff.rainfall_mm !== null ? (
            <>
              <Row label="Assumed rainfall" value={`${num(runoff.rainfall_mm)} mm`} />
              <Row label="Runoff volume" value={volume(runoff.runoff_m3 ?? 0)} />
            </>
          ) : (
            <Row label="Runoff volume" value="add rainfall to estimate" />
          )}
          <Row label="Method" value={runoff.method} />
        </dl>
      </section>

      <section className="card">
        <header>
          <h2>Recommended site</h2>
          <span className="badge clay">{pond_site.rating} · {pond_site.score}</span>
        </header>
        <dl className="rows">
          <Row label="Position" value={coords(pond_site.latitude, pond_site.longitude)} />
          <Row label="Ground level" value={`${num(pond_site.elevation_m, 1)} m`} />
          <Row label="Slope" value={`${num(pond_site.slope_deg, 1)}°`} />
          <Row label="Natural hollow" value={`${num(pond_site.depression_depth_m, 2)} m deep`} />
          <Row
            label="Above the drainage line"
            value={`${num(pond_site.height_above_drainage_m, 1)} m`}
          />
          <Row label="Spills at" value={`${num(pond_site.storage.spill_elevation_m, 1)} m`} />
          <Row label="Water surface" value={`${area(pond_site.storage.surface_area_m2).value} ${area(pond_site.storage.surface_area_m2).unit}`} />
          <Row label="Storage" value={volume(pond_site.storage.volume_m3)} />
        </dl>
        <div className="meters">
          {Object.entries(pond_site.score_breakdown).map(([name, value]) => (
            <div className="meter" key={name}>
              <span>{name}</span>
              <div className="track"><div className="fill" style={{ width: `${value * 100}%` }} /></div>
              <span>{Math.round(value * 100)}</span>
            </div>
          ))}
        </div>
        <ul className="reasons">
          {pond_site.reasons.map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      </section>

      {alternative_sites.length > 0 && (
        <section className="card">
          <header>
            <h2>All candidates</h2>
            <span className="eyebrow">{sites.length} ranked</span>
          </header>
          {sites.map((site) => (
            <button
              key={site.rank}
              className={`alt${site.rank === selected.rank ? ' active' : ''}`}
              onClick={() => onSelect(site)}
            >
              <span className="rank">{site.rank}</span>
              <span>
                <span className="where">{coords(site.latitude, site.longitude)}</span>
                <br />
                {num(site.elevation_m, 1)} m · {num(site.height_above_drainage_m, 1)} m above drainage ·{' '}
                {volume(site.storage.volume_m3)}
              </span>
              <span className="score">{site.score}</span>
            </button>
          ))}
        </section>
      )}

      {channel_structures.length > 0 && (
        <section className="card">
          <header>
            <h2>Not ponds</h2>
            <span className="eyebrow">{channel_structures.length} on drainage lines</span>
          </header>
          <p className="caption" style={{ marginTop: 0 }}>
            These scored well on the terrain but more than{' '}
            {num(watercourses.farm_pond_max_catchment_ha)} ha drains through them, or one ordinary
            storm would fill and overtop them. Either way the water has to be passed on, not just
            held — a waste weir or spillway, and the consent of whoever is downstream.
          </p>
          {channel_structures.map((site) => (
            <div className="alt static" key={`bund-${site.rank}`}>
              <span className="rank">B{site.rank}</span>
              <span>
                <span className="where">{coords(site.latitude, site.longitude)}</span>
                <br />
                {site.structure_label} · {num(site.upstream_hectares, 1)} ha upstream ·{' '}
                {volume(site.storage.volume_m3)}
              </span>
              <span className="score">{site.score}</span>
            </div>
          ))}
          <ul className="reasons">
            {channel_structures[0].reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </section>
      )}

      <section className="card">
        <header>
          <h2>Water already there</h2>
          <span className="eyebrow">
            <Waves size={12} style={{ verticalAlign: '-2px', marginRight: 4 }} />
            {num(watercourses.excluded_fraction * 100, 1)}% withheld
          </span>
        </header>
        <dl className="rows">
          <Row label="River channel" value={`${num(watercourses.river_hectares, 1)} ha`} />
          <Row
            label="Fed from off the map"
            value={`${num(watercourses.truncated_hectares, 1)} ha`}
          />
          <Row label="Floodplain" value={`${num(watercourses.floodplain_hectares, 1)} ha`} />
          <Row label="Nala (too big for a pond)" value={`${num(watercourses.nala_hectares, 1)} ha`} />
          <Row label="Standing water" value={`${num(watercourses.still_water_hectares, 1)} ha`} />
          <Row
            label="Farm pond limit"
            value={`up to ${num(watercourses.farm_pond_max_catchment_ha)} ha catchment`}
          />
          <Row
            label="Counted as river"
            value={`over ${num(watercourses.river_min_catchment_ha)} ha, plus ${num(watercourses.river_buffer_m)} m either side`}
          />
          <Row
            label="Counted as floodplain"
            value={`under ${num(watercourses.river_floodplain_hand_m)} m above a river, or ${num(watercourses.nala_bank_hand_m)} m above a nala`}
          />
        </dl>
        <p className="caption">{watercourses.note}</p>
      </section>

      <section className="card">
        <header>
          <h2>Terrain</h2>
          <span className="eyebrow">{surveyed ? 'reconstructed' : 'downloaded'}</span>
        </header>
        <dl className="rows">
          <Row label="Mapped area" value={`${num(terrain.mapped_area_km2, 2)} km²`} />
          <Row label="Elevation" value={`${num(terrain.min_elevation_m, 1)} – ${num(terrain.max_elevation_m, 1)} m`} />
          <Row label="Relief" value={`${num(terrain.relief_m, 1)} m`} />
          <Row label="Mean slope" value={`${num(terrain.mean_slope_deg, 1)}°`} />
          <Row label="Grid" value={`${terrain.grid.rows} × ${terrain.grid.cols} at ${num(terrain.grid.cell_size_m, 1)} m`} />
          <Row label="Extrapolated" value={`${num(terrain.grid.extrapolated_fraction * 100, 1)}% of cells`} />
        </dl>
      </section>

      <section className="card">
        <header>
          <h2>{surveyed ? 'Source file' : 'Source data'}</h2>
          <span className="eyebrow">{surveyed ? source.format : source.provider}</span>
        </header>
        <dl className="rows">
          {surveyed ? (
            <>
              <Row label="File" value={source.name} />
              <Row label="Contour lines" value={`${num(source.contour_lines)} (${num(source.vertices)} points)`} />
              <Row label="Levels" value={`${source.elevation_levels} at ${num(source.contour_interval_m, 1)} m`} />
            </>
          ) : (
            <>
              <Row label="Area" value={source.name} />
              <Row label="Dataset" value={source.format} />
              <Row label="Sampling" value={`${num(source.sample_spacing_m, 0)} m · ${source.tiles_fetched} tiles at z${source.tile_zoom}`} />
            </>
          )}
          <Row label="Elevation range" value={`${num(source.elevation_min_m, 1)} – ${num(source.elevation_max_m, 1)} m`} />
          <Row label="Analysis" value={result.analysis_id} />
        </dl>
      </section>
    </>
  );
}
