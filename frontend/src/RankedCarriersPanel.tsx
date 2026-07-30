/**
 * U5 — every carrier this broker has used, ranked, with the reasons.
 *
 * Three things this panel deliberately does not do:
 *
 * * **It does not re-sort.** The API ranked by the unrounded weighted sum,
 *   tie-broken by carrier id (PRD section 8). `rank` is printed from the field,
 *   not from an `<ol>` counter, so a list that ever arrived out of order would
 *   show it rather than quietly renumbering itself.
 * * **It does not truncate or filter.** A carrier scoring near zero comes back
 *   last with an accurate reason, and hiding it defeats the point — a silently
 *   short list is indistinguishable from a bug.
 * * **It does not compute.** `score` was rounded once, server-side, half-up
 *   (DECISIONS.md D19). The reasons were generated from the same numbers that
 *   produced it (invariant 2), so they are rendered verbatim and in order.
 *   Nothing here re-adds the signals, re-rounds the score, or turns a stored
 *   ratio into a percentage — `basis` and the reasons already state those.
 */

import { getRecommendations } from "./api";
import { day, equipmentFilter, instant, loads, score as fmtScore, tier, UNKNOWN } from "./format";
import { TierWalkTable } from "./TierWalk";
import type { CarrierScore, Recommendations } from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
}

export function RankedCarriersPanel({ brokerId, loadId }: Props) {
  const recommendations = useApi<Recommendations>(`${brokerId}|${loadId}`, (signal) =>
    getRecommendations(brokerId, loadId, signal),
  );

  return (
    <section>
      <h3>Ranked carriers</h3>
      {recommendations.state === "loading" && <p className="note">Loading carriers…</p>}
      {recommendations.state === "idle" && <p className="note">No load selected.</p>}
      {recommendations.state === "error" && (
        <p className="error">Could not rank carriers: {recommendations.message}</p>
      )}
      {recommendations.state === "ready" && <Ranking ranking={recommendations.data} />}
    </section>
  );
}

function Ranking({ ranking }: { ranking: Recommendations }) {
  return (
    <>
      <p className="provenance">{ranking.basis}</p>

      <dl className="facts">
        <Fact label="As of" value={ranking.as_of} />
        <Fact label="Tier used" value={tier(ranking.tier)} />
        <Fact label="Lane key" value={ranking.lane_key ?? UNKNOWN} />
        <Fact label="Equipment pool" value={equipmentFilter(ranking.equipment_pool)} />
        <Fact label="Loads on this lane" value={loads(ranking.lane_load_count)} />
        {/* The count pair, not a percentage: `lane_on_time_rate` is a raw ratio
            and `basis` above already states it in words. Two renderings of one
            rate is exactly the disagreement invariant 2 forbids. */}
        <Fact
          label="Lane on-time"
          value={`${ranking.lane_on_time_count} of ${ranking.lane_on_time_eligible_count} delivered loads`}
        />
        <Fact label="Carriers scored" value={String(ranking.carriers.length)} />
      </dl>

      <TierWalkTable walk={ranking.walk} />

      {ranking.carriers.length === 0 ? (
        <p className="warn">
          This broker has no carriers on record, so there is nobody to rank for this load.
        </p>
      ) : (
        <div className="carriers">
          {ranking.carriers.map((carrier) => (
            <CarrierCard key={carrier.carrier.source_carrier_id} scored={carrier} />
          ))}
        </div>
      )}
    </>
  );
}

function CarrierCard({ scored }: { scored: CarrierScore }) {
  const { carrier } = scored;
  return (
    <article className="carrier">
      <h4>
        <span className="rank">#{scored.rank}</span>{" "}
        {carrier.name ?? carrier.source_carrier_id}{" "}
        <span className="score">{fmtScore(scored.score)}</span>
      </h4>
      <p className="contact">
        {carrier.phone ?? "no phone on file"} · {carrier.source_carrier_id}
        {carrier.mc_number !== null && <> · MC {carrier.mc_number}</>}
        {carrier.dot_number !== null && <> · DOT {carrier.dot_number}</>}
        {carrier.home_city !== null && (
          <>
            {" "}
            · based in {carrier.home_city}
            {carrier.home_state === null ? "" : `, ${carrier.home_state}`}
          </>
        )}
      </p>
      {/* In the API's order, all of them. PRD section 8 orders these by signal
          with the non-scoring rate note last; re-sorting or cutting to the top
          three would hide the reason a carrier is ranked where it is. */}
      <ul className="reasons">
        {scored.reasons.map((reason, index) => (
          <li key={index}>{reason}</li>
        ))}
      </ul>
      <p className="muted evidence">
        Lane loads {scored.lane_loads} · last {day(scored.last_lane_load_date)} · equipment
        loads {scored.equipment_loads} · on-time {scored.on_time_count}/
        {scored.on_time_eligible_count} · last delivery{" "}
        {scored.last_delivery === null
          ? "unknown"
          : instant(scored.last_delivery.at)}
      </p>
    </article>
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
