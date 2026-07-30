/**
 * The load detail screen — U3's facts, plus the two panels that are still a
 * seam (see `provisional.tsx`). The sync-history panel is U6 and is not here.
 *
 * Every figure on this screen is a field of the load as the API returned it.
 * Nothing is derived: no rate per mile, no margin, no totals.
 */

import { getLoad } from "./api";
import { day, equipment, instant, miles, money, place, pounds, UNKNOWN } from "./format";
import { PriceEstimatePanel, RankedCarriersPanel } from "./provisional";
import type { Load, Stop } from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
  onBack: () => void;
}

export function LoadDetail({ brokerId, loadId, onBack }: Props) {
  const load = useApi<Load>(`${brokerId}|${loadId}`, (signal) => getLoad(brokerId, loadId, signal));

  return (
    <div>
      <button className="link" onClick={onBack}>
        ← back to loads
      </button>

      {load.state === "idle" && <p className="note">No load selected.</p>}
      {load.state === "loading" && <p className="note">Loading load {loadId}…</p>}
      {load.state === "error" && (
        <p className="error">Could not load {loadId}: {load.message}</p>
      )}
      {load.state === "ready" && (
        <>
          <LoadFacts load={load.data} />
          <StopsTable stops={load.data.stops} />
          <PriceEstimatePanel brokerId={brokerId} loadId={loadId} />
          <RankedCarriersPanel brokerId={brokerId} loadId={loadId} />
        </>
      )}
    </div>
  );
}

function LoadFacts({ load }: { load: Load }) {
  return (
    <section>
      <h2>
        {load.load_number ?? load.source_load_id}{" "}
        <span className={load.status === "ACTIVE" ? "status status-active" : "status"}>
          {load.status}
        </span>
      </h2>
      <dl className="facts">
        <Fact label="Source load id" value={load.source_load_id} />
        <Fact label="Equipment" value={equipment(load.equipment)} />
        <Fact label="Weight" value={pounds(load.weight_lbs)} />
        <Fact label="Distance" value={miles(load.distance_miles)} />
        <Fact label="Customer rate" value={money(load.customer_rate)} />
        {/* An ACTIVE load has no carrier rate. `—` is the honest rendering of
            that; `$0.00` would be a different claim entirely. */}
        <Fact label="Carrier rate" value={money(load.carrier_rate)} />
        <Fact label="Carrier" value={load.source_carrier_id ?? UNKNOWN} />
        <Fact label="Customer" value={load.source_customer_id ?? UNKNOWN} />
        <Fact label="Created" value={instant(load.created_at)} />
        <Fact label="Last modified" value={instant(load.last_modified_at)} />
      </dl>
      {load.cargo.length > 0 && (
        <ul className="cargo">
          {load.cargo.map((item, index) => (
            <li key={index}>
              {item.commodity ?? UNKNOWN} · {pounds(item.weight_lbs)} ·{" "}
              {item.pallet_count === null ? UNKNOWN : `${item.pallet_count} pallets`}
            </li>
          ))}
        </ul>
      )}
    </section>
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

/**
 * Every stop in the order the TMS gave them — middle stops included. They are
 * not lane-forming, but they are part of the load and the broker is entitled to
 * see them.
 */
function StopsTable({ stops }: { stops: Stop[] }) {
  return (
    <section>
      <h3>Stops</h3>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Kind</th>
            <th>Location</th>
            <th>Scheduled</th>
            <th>Arrived</th>
            <th>Departed</th>
          </tr>
        </thead>
        <tbody>
          {stops.map((stop) => (
            <tr key={stop.sequence}>
              <td>{stop.sequence}</td>
              <td>{stopKind(stop)}</td>
              <td>
                {stop.location.name && <span className="muted">{stop.location.name} · </span>}
                {place(stop.location)}
                {stop.location.place === null ? (
                  <span className="flag" title="Not matched in the geo table">
                    no geo match
                  </span>
                ) : (
                  <span className="muted"> ({stop.location.place.metro})</span>
                )}
              </td>
              <td>{day(stop.scheduled_date)}</td>
              <td>{instant(stop.actual_arrival)}</td>
              <td>{instant(stop.actual_departure)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function stopKind(stop: Stop): string {
  if (stop.is_pickup && stop.is_drop) return "pickup + drop";
  if (stop.is_pickup) return "pickup";
  if (stop.is_drop) return "drop";
  return UNKNOWN;
}
