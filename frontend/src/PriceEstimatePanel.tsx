/**
 * U4 — what this load should cost, and everything that backs the number.
 *
 * Every figure here is a field of the response. The browser multiplies nothing:
 * `point_usd` is already median-rate × miles, `low_usd`/`high_usd` are already
 * the p25/p75 ends, and `confidence` is already the domain's label. Recomputing
 * any of them would be a second source of truth for a number that exists, which
 * CLAUDE.md invariant 2 calls the worst possible bug in this project.
 *
 * The one place presentation has real content: rates per mile print at four
 * decimals, their stored precision. DECISIONS.md D18 requires an estimate to be
 * reproducible by hand from its own evidence, and `1.81 × 296.0` is not the
 * `$534.28` printed above it — `1.8050 × 296.0` is.
 *
 * Confidence is rendered as a badge whose class carries the level, so a low
 * confidence answer cannot be mistaken for a high one at a glance (invariant 6:
 * low confidence is labeled, never hidden).
 */

import { getPriceEstimate } from "./api";
import {
  confidence,
  day,
  equipment,
  equipmentFilter,
  loads,
  miles,
  money,
  ratePerMile,
  tier,
  UNKNOWN,
} from "./format";
import { TierWalkTable } from "./TierWalk";
import type { PriceEstimate } from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
}

export function PriceEstimatePanel({ brokerId, loadId }: Props) {
  const estimate = useApi<PriceEstimate>(`${brokerId}|${loadId}`, (signal) =>
    getPriceEstimate(brokerId, loadId, signal),
  );

  return (
    <section>
      <h3>Price estimate</h3>
      {estimate.state === "loading" && <p className="note">Loading estimate…</p>}
      {estimate.state === "idle" && <p className="note">No load selected.</p>}
      {estimate.state === "error" && (
        <p className="error">Could not price this load: {estimate.message}</p>
      )}
      {estimate.state === "ready" && <Estimate estimate={estimate.data} />}
    </section>
  );
}

function Estimate({ estimate }: { estimate: PriceEstimate }) {
  return (
    <>
      <p className="headline">
        {/* `null` is not `$0`. A load with no distance, or a broker with no
            history at any rung, has no estimate — and `provenance` says which. */}
        <span className="point">{money(estimate.point_usd)}</span>
        <span className={`confidence confidence-${estimate.confidence}`}>
          {confidence(estimate.confidence)}
        </span>
      </p>
      <p className="range">
        Range {money(estimate.low_usd)} – {money(estimate.high_usd)}{" "}
        <span className="muted">(p25 – p75)</span>
      </p>
      <p className="provenance">{estimate.provenance}</p>

      <dl className="facts">
        <Fact label="Tier used" value={tier(estimate.tier)} />
        <Fact label="Loads backing it" value={loads(estimate.load_count)} />
        <Fact
          label="Loads span"
          value={
            estimate.first_load_date === null && estimate.last_load_date === null
              ? UNKNOWN
              : `${day(estimate.first_load_date)} to ${day(estimate.last_load_date)}`
          }
        />
        <Fact label="Equipment filter" value={equipmentFilter(estimate.equipment_filter)} />
        <Fact label="This load's equipment" value={equipment(estimate.load_equipment)} />
        <Fact label="Distance used" value={miles(estimate.distance_miles)} />
        {/* Printed so the dollars above can be checked by hand: p50 × distance
            is the point estimate, to the cent (DECISIONS.md D18). */}
        <Fact label="Rate p25" value={ratePerMile(estimate.rate_per_mile_p25)} />
        <Fact label="Rate p50 (median)" value={ratePerMile(estimate.rate_per_mile_p50)} />
        <Fact label="Rate p75" value={ratePerMile(estimate.rate_per_mile_p75)} />
      </dl>

      {/* D15: a pool spanning trailer types has a p75 no carrier was ever paid,
          so confidence is capped at medium and the mix is named rather than
          summarized away. The counts are the API's; nothing is added up here. */}
      {estimate.is_heterogeneous && (
        <div className="warn">
          {/* States the fact, not the label. D15's cap is medium, but the
              effective confidence can still be lower (a REGION rung is low
              regardless), and `confidence` above is the only thing entitled to
              say which — a caveat that named a level could contradict it. */}
          <p>
            Mixed equipment pool — the loads behind this estimate are not all the same
            trailer type, so these rates are not like-for-like:
          </p>
          <ul>
            {estimate.equipment_mix.map((line) => (
              <li key={line.equipment}>
                {equipment(line.equipment)} — {loads(line.load_count)}
              </li>
            ))}
          </ul>
        </div>
      )}

      <TierWalkTable walk={estimate.walk} />
    </>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
