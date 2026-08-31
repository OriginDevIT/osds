/**
 * Parser for the search `near` parameter. Pure - no DB, no Node APIs - so it is
 * safe to import from both the page and the Edge middleware.
 *
 * OSDS ships no geocoder, so `near` is coordinates only: `lat,lon`. Anything
 * else is `invalid` and the middleware turns it into a 400.
 */
const COORDS = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;

export type ParsedNear =
  | { readonly kind: "empty" }
  | { readonly kind: "invalid" }
  | { readonly kind: "coords"; readonly lat: number; readonly lon: number };

export function parseNear(near: string | null | undefined): ParsedNear {
  if (near === null || near === undefined || near.trim() === "") {
    return { kind: "empty" };
  }
  const match = COORDS.exec(near);
  if (match === null) return { kind: "invalid" };

  const lat = Number(match[1]);
  const lon = Number(match[2]);
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    return { kind: "invalid" };
  }
  return { kind: "coords", lat, lon };
}
