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
 * ## Source
 *
 * Every declaration here is transcribed from `backend/app/api/schemas.py` — the
 * Pydantic models FastAPI actually serializes — and checked field by field
 * against live responses from a populated database. It is no longer a reading of
 * the PRD: where the two disagreed, the payload won and this file changed.
 */

// ---------------------------------------------------------------------------
// Loads — PRD sections 5 and 10
// ---------------------------------------------------------------------------

/** The four canonical trailer types. `UNKNOWN` is a value, never a gap. */
export type Equipment = "DRY_VAN" | "REEFER" | "FLATBED" | "UNKNOWN";

/**
 * What pool an answer was drawn from: one trailer type, or `ANY`.
 *
 * `ANY` is the D6 filter-skip (the load's own equipment is `UNKNOWN`) or the
 * fourth rung of the walk, which has no filter by definition. Both mean the
 * rates behind the answer are not like-for-like — see `is_heterogeneous`.
 */
export type EquipmentFilter = Equipment | "ANY";

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

/**
 * A carrier as one broker's TMS knows it, plus its last known truck position.
 *
 * `home_city`/`home_state` are null for every TMS A carrier in the fixture —
 * that schema simply does not carry them — so the contact block has to survive
 * having only a phone number.
 */
export interface Carrier {
  source_carrier_id: string;
  name: string | null;
  mc_number: string | null;
  dot_number: string | null;
  phone: string | null;
  home_city: string | null;
  home_state: string | null;
  last_delivery_lat: number | null;
  last_delivery_lon: number | null;
  last_delivery_at: string | null;
}

export interface Customer {
  source_customer_id: string;
  name: string | null;
}

/**
 * One row of the append-only sync log (invariant 3), as it arrived.
 *
 * `raw_json` is the entity exactly as its TMS stated it in that file — not a
 * canonical projection. That is the point of U6: a correction has to be visible
 * *as* a correction, in the source's own vocabulary, next to the file that
 * carried it. A normalized rendering would hide the difference.
 *
 * `entity_type` includes `RATE_LINE`, which is how a TMS B sync that appends
 * money to a load its `loads` array never mentions still appears in that load's
 * history (CLAUDE.md, Known traps).
 */
export interface SyncEvent {
  id: number;
  sync_file: string;
  synced_at: string;
  entity_type: string;
  source_entity_id: string;
  source_load_id: string | null;
  event_seq: number;
  raw_json: Record<string, unknown>;
}

/**
 * `/api/loads/{id}` — a `Load` plus its counterparties and its whole history.
 *
 * Note that the list route returns the bare `Load` and the detail route returns
 * this; the extra three fields are the only difference, and `LoadDetailOut`
 * subclasses `LoadOut` server-side for exactly that reason.
 */
export interface LoadDetail extends Load {
  carrier: Carrier | null;
  customer: Customer | null;
  sync_history: SyncEvent[];
}

// ---------------------------------------------------------------------------
// The tier walk — shared by both answers (invariant 6)
// ---------------------------------------------------------------------------

/** Which rung of the tier walk backed an answer (PRD section 7). */
export type LaneTier = "ZIP3" | "METRO" | "REGION" | "REGION_ANY";

/**
 * One rung: what was asked, what was found, what was decided.
 *
 * `skipped` distinguishes a rung a geo-null load could not *form* from one that
 * was asked and came back thin — the key fields are all null in that case.
 * `verdict` is the finished sentence (`ACCEPTED`, `rejected, 2 < 5`, `skipped,
 * lane end not on the map`); the UI prints it rather than rebuilding it from
 * `load_count` and `accepted`, which would be a second way to say the same
 * thing and a second way to get it wrong.
 */
export interface TierAttempt {
  tier: LaneTier;
  origin_key: string | null;
  dest_key: string | null;
  lane_key: string | null;
  equipment: EquipmentFilter | null;
  load_count: number;
  accepted: boolean;
  skipped: boolean;
  verdict: string;
}

/**
 * Every rung tried, narrow to wide, and the one that won.
 *
 * `tier === null` means nothing cleared `min_sample`. The rungs are still
 * listed: "we have no evidence" is an answer and an empty panel is not.
 *
 * Rungs after the accepted one are absent, not present with a zero count —
 * they were never asked, and claiming a count for them would invent evidence.
 */
export interface TierWalk {
  tier: LaneTier | null;
  load_count: number;
  equipment: EquipmentFilter;
  equipment_filter: EquipmentFilter;
  equipment_filtered: boolean;
  min_sample: number;
  rungs: TierAttempt[];
}

// ---------------------------------------------------------------------------
// Price estimate — PRD section 9
// ---------------------------------------------------------------------------

/** PRD section 9: high (>=15 loads) / medium (5-14) / low (<5, or REGION*). */
export type Confidence = "high" | "medium" | "low";

/** One line of the accepted pool's equipment breakdown (DECISIONS.md D15). */
export interface EquipmentCount {
  equipment: Equipment;
  load_count: number;
}

/**
 * What this load should cost, and exactly what backs the number.
 *
 * Every money field is nullable and each `null` means something other than
 * zero: no rung accepted, or a load with no usable distance. `provenance` — one
 * finished sentence built server-side from these same values — says which.
 *
 * `confidence` is the domain's label and is never re-derived from
 * `load_count`: DECISIONS.md D15 caps a heterogeneous pool at medium, so a
 * client recomputing from the count alone would disagree with the label.
 *
 * The `rate_per_mile_*` fields are `NUMERIC(10,4)` and must be displayed at
 * that precision. DECISIONS.md D18: an estimate has to be reproducible by hand
 * from its own evidence, and a rate rounded to two places no longer multiplies
 * out to the dollars printed beside it (1.8050 x 296.0 = 534.28, but
 * 1.81 x 296.0 = 535.76).
 */
export interface PriceEstimate {
  load_id: string;
  tier: LaneTier | null;
  load_count: number;
  confidence: Confidence;
  rate_per_mile_p25: number | null;
  rate_per_mile_p50: number | null;
  rate_per_mile_p75: number | null;
  point_usd: number | null;
  low_usd: number | null;
  high_usd: number | null;
  distance_miles: number | null;
  first_load_date: string | null;
  last_load_date: string | null;
  equipment_filter: EquipmentFilter;
  load_equipment: Equipment;
  /** Non-empty only when the accepted pool was unfiltered. Commonest first. */
  equipment_mix: EquipmentCount[];
  is_heterogeneous: boolean;
  provenance: string;
  walk: TierWalk;
}

// ---------------------------------------------------------------------------
// Recommendations — PRD section 8
// ---------------------------------------------------------------------------

/** One signal's arithmetic and the sentence generated from it (invariant 2). */
export interface Signal {
  name: string;
  label: string;
  weight: number;
  observed: number | null;
  value: number;
  contribution: number;
  reason: string;
}

/**
 * Where a carrier's truck last ended up, and which load put it there.
 *
 * `lat`/`lon` are null when the delivery went to a town the offline geo table
 * does not know — we have the load and the place name, and nothing to measure
 * from, so `deadhead_miles` is null too and the deadhead reason says so. That is
 * a different response from `last_delivery: null`, which means the carrier has
 * never delivered anything for this broker (DECISIONS.md D23).
 */
export interface LastDelivery {
  source_load_id: string;
  lat: number | null;
  lon: number | null;
  at: string | null;
  location: StopLocation;
}

/**
 * One ranked carrier.
 *
 * `score` is the number to display: the weighted sum, rounded once, half-up, in
 * the API (PRD section 8's presentation contract, DECISIONS.md D19). Do not
 * re-round it and do not re-add `signals` — their `contribution` values sum to
 * `score_exact`, which is what the ordering used, and computing it again in the
 * browser is a second source of truth for a number that already exists.
 *
 * `reasons` is ordered by PRD section 8's signal order with the non-scoring
 * rate note last. Render it as given: no re-sorting, no truncating to three.
 */
export interface CarrierScore {
  rank: number;
  carrier: Carrier;
  score: number;
  score_exact: number;
  reasons: string[];
  signals: Signal[];
  lane_loads: number;
  last_lane_load_date: string | null;
  days_since_lane_load: number | null;
  equipment_loads: number;
  deadhead_miles: number | null;
  last_delivery: LastDelivery | null;
  on_time_count: number;
  on_time_eligible_count: number;
  observed_on_time_rate: number | null;
  avg_rate_per_mile: number | null;
}

/**
 * Every carrier this broker has used, best first. Nobody is dropped — a carrier
 * with no lane history, no known truck position and the wrong trailer still
 * comes back, last, with reasons saying which of those is true.
 *
 * `as_of` is the Central date of the newest ingested sync file, not the wall
 * clock. `basis` is the finished sentence for the header; `lane_on_time_rate`
 * is a raw ratio (0.9166666666666666) that the UI does *not* turn into a
 * percentage — `basis` already states it, and two renderings of one rate is
 * exactly the disagreement invariant 2 forbids.
 */
export interface Recommendations {
  load_id: string;
  as_of: string;
  tier: LaneTier | null;
  lane_key: string | null;
  equipment_pool: EquipmentFilter;
  lane_load_count: number;
  lane_on_time_count: number;
  lane_on_time_eligible_count: number;
  lane_on_time_rate: number | null;
  basis: string;
  walk: TierWalk;
  carriers: CarrierScore[];
}

// ---------------------------------------------------------------------------
// The shared carrier pool — Phase 11, DECISIONS.md D17
// ---------------------------------------------------------------------------

/** `/api/pool/opt-in`. Off by default; a broker can only ask about itself. */
export interface PoolOptIn {
  broker_id: string;
  opted_in: boolean;
}

/** The tiers the pool answers at. `ZIP3` never crosses — it is roughly a facility. */
export type PoolTier = "METRO" | "REGION";

/**
 * Depth of another broker's relationship with a carrier, as a **band**.
 *
 * Never a count. Below five loads there is no row at all, so there is no
 * `"0-4"` — a suppressed carrier is absent, not zero.
 */
export type LoadBand = "5-9" | "10-19" | "20-49" | "50+";

/**
 * On-time as a band, or `null` when nothing that carrier ran on the lane has
 * delivered yet. `null` is a third state and is not `"<75"`.
 */
export type OnTimeBand = "90+" | "75-89" | "<75";

/**
 * A carrier as the pool knows them: identity and bands, and nothing else.
 *
 * There is no rate field here and there is no rate field on the API model this
 * is transcribed from, because the projection it is read through has no rate
 * column. If a future version of this interface grows one, something three
 * layers down has been undone — do not add it to make a payload typecheck.
 *
 * `equipment_operated` is what the pool has seen them pull, subject to the same
 * five-load floor, so it understates a fleet and never overstates one.
 */
export interface PoolCarrier {
  mc_number: string;
  dot_number: string | null;
  name: string | null;
  phone: string | null;
  home_city: string | null;
  home_state: string | null;
  tier: PoolTier;
  lane_key: string;
  equipment: EquipmentFilter;
  load_band: LoadBand;
  on_time_band: OnTimeBand | null;
  active_recently: boolean;
  equipment_operated: Equipment[];
  /** How many *other* opted-in brokers run this carrier. Never which ones. */
  contributor_count: number;
}

/**
 * One scored pool carrier.
 *
 * `score` is on the ranking's 0-100 scale and is **not comparable with it**:
 * every signal was scored at the weakest end of a band and deadhead is
 * structurally zero, because truck position does not cross the boundary. So it
 * is a lower bound, which is why these are rendered as a separate labeled
 * section rather than merged into the ranked list. Render `reasons` as given —
 * each line says which band produced it.
 *
 * `source` is a constant `"shared_pool"` carried on every row, so a pool
 * carrier that ever appeared somewhere it should not would say so itself.
 */
export interface PoolCarrierScore {
  source: string;
  rank: number;
  carrier: PoolCarrier;
  score: number;
  score_exact: number;
  reasons: string[];
  signals: Signal[];
}

/**
 * `/api/loads/{id}/pool-carriers` — the labeled second section, or the reason
 * there isn't one.
 *
 * `opted_in` and `eligible` fail for different reasons and are reported
 * separately: not in the pool at all, versus in the pool but asking about a
 * load that is not `ACTIVE`. `basis` states whichever it is in words, so the
 * panel prints that rather than assembling its own sentence from the booleans.
 */
export interface PoolSection {
  load_id: string;
  as_of: string;
  opted_in: boolean;
  eligible: boolean;
  tier: PoolTier | null;
  lane_key: string | null;
  equipment_pool: EquipmentFilter | null;
  basis: string;
  carriers: PoolCarrierScore[];
}
