/**
 * Display formatting. Everything here turns a value the API already decided
 * into characters on screen — none of it decides a value.
 *
 * The line matters. Formatting `1234.5` as `$1,234.50` is presentation.
 * Dividing a carrier rate by a distance to show `$/mi` would be a second
 * implementation of a number the backend already computes, and CLAUDE.md
 * invariant 2 calls a displayed number that can disagree with its score the
 * worst possible bug in this project. So there is no arithmetic in this file.
 */

import type { Equipment, Load, Stop, StopLocation } from "./types";

/** What we print where the API said "unknown". Never "0", never blank. */
export const UNKNOWN = "—";

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const DECIMAL = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** `null` prints as `—`, and `0` prints as `$0.00`. They are different facts. */
export function money(value: number | null): string {
  return value === null ? UNKNOWN : USD.format(value);
}

export function miles(value: number | null): string {
  return value === null ? UNKNOWN : `${DECIMAL.format(value)} mi`;
}

export function pounds(value: number | null): string {
  return value === null ? UNKNOWN : `${DECIMAL.format(value)} lb`;
}

/** A `YYYY-MM-DD` local calendar date, passed straight through. */
export function day(value: string | null): string {
  return value ?? UNKNOWN;
}

/**
 * An ISO instant, rendered in UTC and labelled as such.
 *
 * Deliberately not `toLocaleString()`: the backend normalizes every timestamp
 * to UTC, and re-rendering it in whatever zone the reviewer's laptop is set to
 * would make a load's times unverifiable against the sync files it came from.
 */
export function instant(value: string | null): string {
  if (value === null) return UNKNOWN;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return `${parsed.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

const EQUIPMENT_LABELS: Record<Equipment, string> = {
  DRY_VAN: "Dry van",
  REEFER: "Reefer",
  FLATBED: "Flatbed",
  UNKNOWN: "Unknown",
};

/** `UNKNOWN` is spelled out, not blanked — it is a value the TMS gave us. */
export function equipment(value: Equipment): string {
  return EQUIPMENT_LABELS[value] ?? value;
}

/**
 * A stop's place, as the TMS wrote it.
 *
 * Always built from the raw `city`/`state`/`zip`, never from `location.place`,
 * so a geo-null stop reads exactly the same as a resolved one. Geo-null means
 * excluded from lane statistics; it does not mean hidden from the broker.
 */
export function place(location: StopLocation): string {
  const town = [location.city, location.state].filter(Boolean).join(", ");
  const label = location.zip ? `${town} ${location.zip}`.trim() : town;
  return label || UNKNOWN;
}

/**
 * The lane ends: first pickup and last drop.
 *
 * This mirrors `Load.origin` / `Load.destination` in
 * `backend/app/domain/model.py` — the same rule, expressed twice, which is one
 * time too many. It is here only because the `/api/loads` payload is not
 * written yet; if it ships explicit `origin`/`destination` fields, delete this
 * and read them. It picks *stops*, not numbers, so the duplication cannot make
 * a displayed figure disagree with the backend.
 */
export function laneEnds(load: Load): { origin: Stop | null; destination: Stop | null } {
  const origin = load.stops.find((stop) => stop.is_pickup) ?? null;
  const destination = [...load.stops].reverse().find((stop) => stop.is_drop) ?? null;
  return { origin, destination };
}

/** `Grand Prairie, TX 75050 -> Katy, TX 77449`, for the list's lane column. */
export function lane(load: Load): string {
  const { origin, destination } = laneEnds(load);
  const from = origin ? place(origin.location) : UNKNOWN;
  const to = destination ? place(destination.location) : UNKNOWN;
  return `${from} → ${to}`;
}

/** True when either lane end failed to resolve against the offline geo table. */
export function hasGeoNullLaneEnd(load: Load): boolean {
  const { origin, destination } = laneEnds(load);
  return (
    (origin !== null && origin.location.place === null) ||
    (destination !== null && destination.location.place === null)
  );
}
