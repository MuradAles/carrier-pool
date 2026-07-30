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

import type {
  Confidence,
  Equipment,
  EquipmentFilter,
  LaneTier,
  Load,
  Stop,
  StopLocation,
} from "./types";

/** What we print where the API said "unknown". Never "0", never blank. */
export const UNKNOWN = "—";

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const DECIMAL = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/**
 * Rates per mile print at their stored precision — four decimals, always.
 *
 * `lane_stats.rate_per_mile_p*` is `NUMERIC(10,4)` and DECISIONS.md D18 says an
 * estimate must be reproducible by hand from its own evidence. Trimming
 * `1.8050` to `1.81` breaks that: `1.81 × 296.0` is `$535.76`, and the panel
 * beside it prints `$534.28`. Padding to four places cannot change a value the
 * API already sent at four places, so this is presentation, not rounding.
 */
const RATE = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
});

/**
 * The published score, at the one decimal the API rounded it to.
 *
 * PRD section 8 rounds the weighted sum **once**, half-up, server-side, so this
 * only pads `84` to `84.0` — it never rounds. Recomputing or re-rounding a
 * score in the browser is the second source of truth D19 exists to prevent.
 */
const SCORE = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** `loads.distance_miles` is `NUMERIC(10,2)`; see `miles`. */
const MILES = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 2,
});

/** `null` prints as `—`, and `0` prints as `$0.00`. They are different facts. */
export function money(value: number | null): string {
  return value === null ? UNKNOWN : USD.format(value);
}

/**
 * Distance at its stored precision — `loads.distance_miles` is `NUMERIC(10,2)`.
 *
 * Whole miles would read more cleanly and would be wrong, because this figure
 * is a *multiplicand*: 293.4 mi shown as "293 mi" beside a median of
 * $2.5300/mi invites a rep to compute $741.29 and find the panel claiming
 * $742.30. Same DECISIONS.md D18 argument as the rate itself, so the two are
 * displayed at matching precision and one formatter serves every screen.
 */
export function miles(value: number | null): string {
  return value === null ? UNKNOWN : `${MILES.format(value)} mi`;
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

export function ratePerMile(value: number | null): string {
  return value === null ? UNKNOWN : `$${RATE.format(value)}/mi`;
}

/** The published 0–100 score, exactly as the API rounded it. */
export function score(value: number): string {
  return SCORE.format(value);
}

/** A plain integer, for load counts. `0 loads` is a fact, not a gap. */
export function loads(value: number): string {
  return `${DECIMAL.format(value)} ${value === 1 ? "load" : "loads"}`;
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
 * The pool an answer was drawn from. `ANY` is spelled out rather than shown as
 * a bare token, because "we did not filter by equipment" is the caveat a reader
 * has to notice — it is the D15 case and the reason confidence is capped.
 */
export function equipmentFilter(value: EquipmentFilter | null): string {
  if (value === null) return UNKNOWN;
  if (value === "ANY") return "All equipment types (no filter)";
  return EQUIPMENT_LABELS[value] ?? value;
}

const TIER_LABELS: Record<LaneTier, string> = {
  ZIP3: "ZIP3 (3-digit zip pair)",
  METRO: "METRO (metro pair)",
  REGION: "REGION (region pair)",
  REGION_ANY: "REGION_ANY (region pair, no equipment filter)",
};

/** `null` means no rung cleared the minimum — say so, do not print a blank. */
export function tier(value: LaneTier | null): string {
  return value === null ? "none accepted" : (TIER_LABELS[value] ?? value);
}

const CONFIDENCE_LABELS: Record<Confidence, string> = {
  high: "HIGH CONFIDENCE",
  medium: "MEDIUM CONFIDENCE",
  low: "LOW CONFIDENCE",
};

export function confidence(value: Confidence): string {
  return CONFIDENCE_LABELS[value] ?? value.toUpperCase();
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
