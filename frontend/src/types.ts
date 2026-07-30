/**
 * The shapes the API returns. Hand-written, not generated — but written against
 * the backend's real vocabulary, not against what the UI wishes it received.
 *
 * Two rules this file exists to enforce:
 *
 * 1. **Nullable in the API is nullable here.** An `ACTIVE` load has no carrier
 *    rate; `backend/app/domain/model.py` types money as `float | None` with no
 *    default precisely so "not known yet" and "$0" stay distinguishable. Typing
 *    `carrier_rate` as `number` would let the UI render `$0.00` for a rate
 *    nobody has agreed to yet, re-introducing the conflation the backend went
 *    out of its way to avoid.
 * 2. **A geo-null location is still a location.** `StopLocation.place === null`
 *    means the city/state/zip did not resolve against the offline geo table.
 *    That excludes the load from *lane statistics*; it does not hide the stop.
 *    The raw `city`/`state`/`zip` are always present and always rendered.
 *
 * ## Settled vs provisional
 *
 * Everything above the PROVISIONAL banner is derived from shipped backend code:
 * `backend/app/repository/schema.sql` (the `brokers` and `loads` tables) and
 * `backend/app/domain/model.py` (the canonical `Load` / `Stop` / `CargoItem`),
 * plus PRD sections 5 and 10.
 *
 * Everything below the banner describes endpoints that **do not exist yet** —
 * scoring (PRD section 8) and pricing (section 9) are two phases away, and
 * `/api/loads/{id}/recommendations` and `/api/loads/{id}/price-estimate` still
 * return 501. Those types are the minimum the UI needs, guessed at field level.
 * Nothing renders them yet: see `provisional.tsx`, which is the single place
 * they will be bound to the real payloads.
 */

// ---------------------------------------------------------------------------
// Settled — PRD sections 5 and 10
// ---------------------------------------------------------------------------

/** The four canonical trailer types. `UNKNOWN` is a value, never a gap. */
export type Equipment = "DRY_VAN" | "REEFER" | "FLATBED" | "UNKNOWN";

export type LoadStatus =
  | "PLANNED"
  | "ACTIVE"
  | "COVERED"
  | "IN_TRANSIT"
  | "DELIVERED"
  | "COMPLETED";

/** Lifecycle order, which is the order the status filter offers them in. */
export const LOAD_STATUSES: readonly LoadStatus[] = [
  "PLANNED",
  "ACTIVE",
  "COVERED",
  "IN_TRANSIT",
  "DELIVERED",
  "COMPLETED",
];

/** `/api/health` — all three fields are free text meant for a human. */
export interface Health {
  status: string;
  database: string;
  data_dir: string;
}

/** A tenant. `/api/brokers` is the one route with no broker binding. */
export interface Broker {
  id: string;
  name: string;
  tms_type: string;
}

/** A resolved row of the offline geo table. Absent from a geo-null stop. */
export interface Place {
  city: string;
  state: string;
  zip: string;
  lat: number;
  lon: number;
  metro: string;
}

/**
 * Where a stop is, as the TMS said it, plus what geo made of it.
 * `place === null` is geo-null — displayable, not lane-forming.
 */
export interface StopLocation {
  city: string | null;
  state: string | null;
  zip: string | null;
  name: string | null;
  place: Place | null;
}

/**
 * One stop, in the order the TMS gave them. `sequence` is 1-based.
 * `scheduled_date` is a local (US Central) calendar date, `YYYY-MM-DD`; the
 * `window_*` and `actual_*` fields are ISO-8601 UTC instants.
 */
export interface Stop {
  sequence: number;
  is_pickup: boolean;
  is_drop: boolean;
  location: StopLocation;
  scheduled_date: string | null;
  window_start: string | null;
  window_end: string | null;
  actual_arrival: string | null;
  actual_departure: string | null;
}

/** Only TMS C splits cargo out. `weight_lbs` is already normalized to pounds. */
export interface CargoItem {
  commodity: string | null;
  weight_lbs: number | null;
  pallet_count: number | null;
}

/**
 * A load in canonical form — the `loads` row plus its ordered stops.
 *
 * `source_load_id` is the identifier the UI addresses a load by: it is the
 * natural key inside a broker (`UNIQUE (broker_id, source_load_id)`), and every
 * load route is broker-scoped anyway.
 *
 * There is deliberately no `rate_per_mile` field in use here even though the
 * `loads` table has a generated column for it. If the API sends one, add it and
 * render it — but the browser must never divide `carrier_rate` by
 * `distance_miles` itself. A number recomputed in the UI is a second source of
 * truth, and the one it will eventually disagree with is the score.
 */
export interface Load {
  source_load_id: string;
  load_number: string | null;
  status: LoadStatus;
  equipment: Equipment;
  stops: Stop[];
  cargo: CargoItem[];
  weight_lbs: number | null;
  distance_miles: number | null;
  customer_rate: number | null;
  carrier_rate: number | null;
  source_carrier_id: string | null;
  source_customer_id: string | null;
  created_at: string | null;
  last_modified_at: string | null;
}

// ---------------------------------------------------------------------------
// PROVISIONAL — endpoints that do not exist yet
//
// `/api/loads/{id}/recommendations` and `/api/loads/{id}/price-estimate` return
// 501 today. The field names below are this file's best reading of PRD sections
// 8 and 9; they are NOT confirmed against a running endpoint, and they are the
// only types in this file that are not.
//
// When the real endpoints land: reconcile these declarations and `provisional.tsx`.
// Nothing else in the frontend imports them, so that is the whole diff.
// ---------------------------------------------------------------------------

/** Which rung of the tier walk backed an answer (PRD section 7). */
export type LaneTier = "ZIP3" | "METRO" | "REGION" | "REGION_ANY";

/** PRD section 9: high (>=15 loads) / medium (5-14) / low (<5, or REGION*). */
export type Confidence = "high" | "medium" | "low";

/**
 * PRD section 9. `point_usd` is median rate-per-mile x load miles, computed
 * server-side; `low_usd`/`high_usd` are the p25/p75 ends of the same walk.
 *
 * All three are nullable because a load with no distance, or a broker with no
 * history at any tier, has no estimate — and "no estimate" must not render as
 * `$0`. `confidence` and `tier` are always present: invariant 6 says every
 * answer reports which tier it used and how many loads backed it, so an
 * estimate that cannot say is not an estimate.
 */
export interface PriceEstimate {
  point_usd: number | null;
  low_usd: number | null;
  high_usd: number | null;
  confidence: Confidence;
  tier: LaneTier;
  load_count: number;
  /** The one-line "median of 22 loads on DFW -> Houston, dry van" sentence. */
  provenance: string;
}

/**
 * One ranked carrier (PRD section 8). `reasons` is an ordered list of finished
 * sentences generated server-side from the same numbers that produced `score`
 * (invariant 2) — the UI renders them in the order given, does not truncate to
 * the top three, and does not drop a carrier whose score is zero.
 */
export interface CarrierRecommendation {
  source_carrier_id: string;
  name: string | null;
  phone: string | null;
  score: number;
  reasons: string[];
}

export interface Recommendations {
  tier: LaneTier;
  carriers: CarrierRecommendation[];
}
