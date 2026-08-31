/** Number formatting shared by the report panel and the map popups. */

export const num = (value: number, digits = 0): string =>
  value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

/** Metres squared, shown in whichever unit keeps the number readable. */
export function area(m2: number): { value: string; unit: string } {
  if (m2 >= 1_000_000) return { value: num(m2 / 1_000_000, 2), unit: 'km²' };
  if (m2 >= 10_000) return { value: num(m2 / 10_000, 2), unit: 'ha' };
  return { value: num(m2), unit: 'm²' };
}

export function distance(m: number): string {
  return m >= 1000 ? `${num(m / 1000, 2)} km` : `${num(m)} m`;
}

export function volume(m3: number): string {
  return m3 >= 1_000_000 ? `${num(m3 / 1_000_000, 2)} Mm³` : `${num(m3)} m³`;
}

export const coords = (lat: number, lon: number): string => `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
