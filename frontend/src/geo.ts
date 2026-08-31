/**
 * The same degrees-to-metres arithmetic the backend uses, for the one thing the
 * browser has to work out on its own: the box an area request will cover, drawn
 * before the request is made.
 */

import type { Bounds } from './types';

/** Metres per degree of latitude and of longitude, at this latitude. */
function metresPerDegree(lat: number): [number, number] {
  const rad = (lat * Math.PI) / 180;
  const perLat = 111132.92 - 559.82 * Math.cos(2 * rad) + 1.175 * Math.cos(4 * rad);
  const perLon = 111412.84 * Math.cos(rad) - 93.5 * Math.cos(3 * rad);
  return [perLat, Math.max(1, perLon)];
}

/** The square of ground `sideKm` across, centred on a point. */
export function bboxAround(lat: number, lon: number, sideKm: number): Bounds {
  const [perLat, perLon] = metresPerDegree(lat);
  const halfLat = (sideKm * 1000) / 2 / perLat;
  const halfLon = (sideKm * 1000) / 2 / perLon;
  return {
    south: Math.max(-89, lat - halfLat),
    west: Math.max(-180, lon - halfLon),
    north: Math.min(89, lat + halfLat),
    east: Math.min(180, lon + halfLon),
  };
}
