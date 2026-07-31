/**
 * The load detail screen: who to call, and what to offer.
 *
 * Reading order is the rep's order. The load's own facts first, then what it
 * should cost with the lane definition that produced it beside it, then the
 * ranked carriers, then the shared pool, then the stops and the raw arrival
 * history for anyone who needs to audit a figure.
 *
 * Every number on this screen is a field of a response as the API returned it.
 * Nothing is derived: no rate per mile, no margin, no totals, no re-rounding.
 *
 * The panels below the facts fetch their own data and each says when it is
 * loading or has failed — one panel being unavailable never blanks the others,
 * and a blank panel is indistinguishable from a bug.
 */

import { getLoad, getPriceEstimate, getRecommendations } from "./api";
import {
  confidence as confidenceLabel,
  day,
  equipment,
  instant,
  laneEnds,
  miles,
  money,
  place,
  pounds,
  score as fmtScore,
  UNKNOWN,
} from "./format";
import { PoolCarriersPanel } from "./PoolCarriersPanel";
import { PriceEstimatePanel } from "./PriceEstimatePanel";
import { RankedCarriersPanel } from "./RankedCarriersPanel";
import { SyncHistoryPanel } from "./SyncHistoryPanel";
import type {
  LoadDetail as Detail,
  PriceEstimate,
  Recommendations,
  Stop,
} from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
  onBack: () => void;
}

export function LoadDetail({ brokerId, loadId, onBack }: Props) {
  const load = useApi<Detail>(`${brokerId}|${loadId}`, (signal) =>
    getLoad(brokerId, loadId, signal),
  );

  return (
    <div>
      <button className="link backlink" onClick={onBack}>
        ← back to loads
      </button>

      {load.state === "idle" && <p className="state">No load selected.</p>}
      {load.state === "loading" && <p className="state">Loading load {loadId}…</p>}
      {load.state === "error" && (
        <p className="state error">
          Could not load {loadId}: {load.message}
        </p>
      )}
      {load.state === "ready" && (
        <>
          <LoadFacts load={load.data} />
          <CoverageAnswer brokerId={brokerId} loadId={loadId} />
          <PriceEstimatePanel
            brokerId={brokerId}
            loadId={loadId}
            customerRate={load.data.customer_rate}
          />
          <RankedCarriersPanel brokerId={brokerId} loadId={loadId} />
          {/* Below the ranking, always separate from it: these carriers are
              scored from bands, not from this broker's own numbers. */}
          <PoolCarriersPanel brokerId={brokerId} loadId={loadId} />
          <StopsPanel stops={load.data.stops} cargo={load.data.cargo} />
          <SyncHistoryPanel
            events={load.data.sync_history}
            createdAt={load.data.created_at}
            lastModifiedAt={load.data.last_modified_at}
          />
        </>
      )}
    </div>
  );
}

/**
 * The two answers, before the evidence for them.
 *
 * The screen exists to answer "who do I call" and "what do I pay", and both
 * used to sit roughly 1300px apart with the carrier below the fold. This puts
 * them together, at the top, in the order the rep asks them.
 *
 * Nothing here is new information: every value is a field of the same two
 * responses the panels below render in full, and both of those panels stay
 * exactly as they are. This is the answer; they are the working. It fetches
 * independently rather than being handed data, so a failure here still leaves
 * the panels below able to answer for themselves.
 */
function CoverageAnswer({ brokerId, loadId }: { brokerId: string; loadId: string }) {
  const key = `${brokerId}|${loadId}`;
  const ranking = useApi<Recommendations>(key, (signal) =>
    getRecommendations(brokerId, loadId, signal),
  );
  const price = useApi<PriceEstimate>(key, (signal) =>
    getPriceEstimate(brokerId, loadId, signal),
  );

  // rank 1 as the API ordered it, not a maximum computed here.
  const top = ranking.state === "ready" ? (ranking.data.carriers[0] ?? null) : null;
  const est = price.state === "ready" ? price.data : null;
  const failed = ranking.state === "error" && price.state === "error";

  if (failed) return null;

  return (
    <section className="answer" aria-label="Recommended action">
      <div className="answer-cell">
        <h3>Call first</h3>
        {top === null ? (
          <p className="answer-none">
            {ranking.state === "ready"
              ? "No carriers on record for this broker."
              : "Ranking…"}
          </p>
        ) : (
          <>
            <p className="answer-lead">{top.carrier.name ?? top.carrier.source_carrier_id}</p>
            <p className="answer-sub mono">{top.carrier.phone ?? "no phone on file"}</p>
            <p className="answer-meta">
              <b>{fmtScore(top.score)}</b> of 100
              {ranking.state === "ready" && ranking.data.tier !== null && (
                <> · scored on {ranking.data.tier} history</>
              )}
            </p>
          </>
        )}
      </div>

      <div className="answer-cell">
        <h3>Expect to pay</h3>
        {est === null || est.point_usd === null ? (
          <p className="answer-none">
            {price.state === "ready" ? "No estimate for this load." : "Pricing…"}
          </p>
        ) : (
          <>
            <p className="answer-lead">{money(est.point_usd)}</p>
            <p className="answer-sub">
              {money(est.low_usd)} to {money(est.high_usd)}
            </p>
            <p className="answer-meta">
              {confidenceLabel(est.confidence)} · median of {est.load_count} loads
            </p>
          </>
        )}
      </div>

      <p className="answer-foot">
        Both figures come from the panels below, which show the lane they were
        drawn from and every number behind them.
      </p>
    </section>
  );
}

function LoadFacts({ load }: { load: Detail }) {
  const { origin, destination } = laneEnds(load);
  const isActive = load.status === "ACTIVE";

  return (
    <section className="panel">
      <div className="load-hd">
        <span className="id mono">{load.load_number ?? load.source_load_id}</span>
        <span className={isActive ? "chip active" : "chip mute"}>{load.status}</span>
        <span className="lane">
          {origin ? place(origin.location) : UNKNOWN}
          <span className="arrow"> → </span>
          {destination ? place(destination.location) : UNKNOWN}
        </span>
        <span className="chip mute">{equipment(load.equipment)}</span>
      </div>

      <dl className="facts">
        <Fact label="Distance" value={miles(load.distance_miles)} />
        <Fact label="Weight" value={pounds(load.weight_lbs)} />
        <Fact label="Pickup" value={day(origin?.scheduled_date ?? null)} />
        <Fact label="Delivery" value={day(destination?.scheduled_date ?? null)} />
        <Fact label="Customer rate" value={money(load.customer_rate)} />
        {/* An ACTIVE load has no carrier rate. `—` is the honest rendering of
            that; `$0.00` would be a different claim entirely. */}
        <Fact
          label="Carrier rate"
          value={money(load.carrier_rate)}
          unit={load.carrier_rate === null ? (isActive ? "not yet known" : "none recorded") : null}
        />
        {/* An unassigned load has no carrier at all, which is a different fact
            from a carrier whose TMS row carries no name. Both print as `—`
            here, so the id is shown alongside rather than instead. */}
        <Fact label="Carrier" value={party(load.carrier?.name ?? null, load.source_carrier_id)} />
        <Fact
          label="Customer"
          value={party(load.customer?.name ?? null, load.source_customer_id)}
        />
      </dl>
    </section>
  );
}

/** `Name (id)`, or the bare id, or `—` when the load names nobody. */
function party(name: string | null, sourceId: string | null): string {
  if (sourceId === null) return UNKNOWN;
  return name === null ? sourceId : `${name} (${sourceId})`;
}

function Fact({
  label,
  value,
  unit = null,
}: {
  label: string;
  value: string;
  unit?: string | null;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={value === UNKNOWN ? "dash" : undefined}>
        {value}
        {unit !== null && <span className="u"> {unit}</span>}
      </dd>
    </div>
  );
}

/**
 * Every stop in the order the TMS gave them — middle stops included. They are
 * not lane-forming, but they are part of the load and the broker is entitled to
 * see them.
 */
function StopsPanel({ stops, cargo }: { stops: Stop[]; cargo: Detail["cargo"] }) {
  return (
    <section className="panel">
      <div className="panel-hd">
        <h3>Stops</h3>
        <span className="sub">
          in TMS order · first pickup and last drop form the lane, middle stops do not
        </span>
      </div>
      <div className="tablewrap">
        <table className="grid">
          <thead>
            <tr>
              <th style={{ width: "44px" }}>#</th>
              <th style={{ width: "120px" }}>Kind</th>
              <th>Location</th>
              <th style={{ width: "112px" }}>Scheduled</th>
              <th style={{ width: "230px" }}>Window (UTC)</th>
              <th style={{ width: "150px" }}>Arrived</th>
              <th style={{ width: "150px" }}>Departed</th>
            </tr>
          </thead>
          <tbody>
            {stops.map((stop) => (
              <tr key={stop.sequence}>
                <td>{stop.sequence}</td>
                <td>{stopKind(stop)}</td>
                <td>
                  {stop.location.name && <span className="muted">{stop.location.name} · </span>}
                  {/* Always the raw city/state/zip, resolved or not. A geo-null
                      stop is excluded from lane statistics, which is not the
                      same as hidden — but it must not read like a resolved one,
                      so the flag rides next to the value, not in a footnote. */}
                  {place(stop.location)}
                  {stop.location.place === null ? (
                    <span
                      className="flag"
                      title="No row in the offline geo table for this city/state/zip"
                    >
                      not on the map — excluded from lane statistics
                    </span>
                  ) : (
                    <span className="muted"> · {stop.location.place.metro}</span>
                  )}
                </td>
                <td>{day(stop.scheduled_date)}</td>
                <td>
                  {stop.window_start === null && stop.window_end === null
                    ? UNKNOWN
                    : `${instant(stop.window_start)} – ${instant(stop.window_end)}`}
                </td>
                <td>{instant(stop.actual_arrival)}</td>
                <td>{instant(stop.actual_departure)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cargo.length > 0 && (
        <div className="note">
          <b>Cargo</b> —{" "}
          {cargo
            .map(
              (item) =>
                `${item.commodity ?? UNKNOWN}, ${pounds(item.weight_lbs)}, ${
                  item.pallet_count === null ? UNKNOWN : `${item.pallet_count} pallets`
                }`,
            )
            .join(" · ")}
        </div>
      )}
    </section>
  );
}

function stopKind(stop: Stop): string {
  if (stop.is_pickup && stop.is_drop) return "pickup + drop";
  if (stop.is_pickup) return "pickup";
  if (stop.is_drop) return "drop";
  return UNKNOWN;
}
