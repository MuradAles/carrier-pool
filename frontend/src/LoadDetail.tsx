/**
 * The load detail screen: U3's facts and stops, then the price estimate (U4),
 * the ranked carriers (U5), the shared pool section (S4) and the sync history
 * (U6).
 *
 * Every figure on this screen is a field of a response as the API returned it.
 * Nothing is derived: no rate per mile, no margin, no totals, no re-rounding.
 *
 * The four panels below the facts fetch or receive their own data, and each
 * says so when it is loading or has failed — one panel being unavailable never
 * blanks the others, and a blank panel is indistinguishable from a bug.
 */

import { getLoad } from "./api";
import { day, equipment, instant, miles, money, place, pounds, UNKNOWN } from "./format";
import { PoolCarriersPanel } from "./PoolCarriersPanel";
import { PriceEstimatePanel } from "./PriceEstimatePanel";
import { RankedCarriersPanel } from "./RankedCarriersPanel";
import { SyncHistoryPanel } from "./SyncHistoryPanel";
import type { LoadDetail as Detail, Stop } from "./types";
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
          {/* Below the ranking, always separate from it: these carriers are
              scored from bands, not from this broker's own numbers. */}
          <PoolCarriersPanel brokerId={brokerId} loadId={loadId} />
          <SyncHistoryPanel events={load.data.sync_history} />
        </>
      )}
    </div>
  );
}

function LoadFacts({ load }: { load: Detail }) {
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
        {/* Full stored precision, not whole miles: this is the figure the price
            panel multiplies a rate by, and the two must agree to the cent. */}
        <Fact label="Distance" value={miles(load.distance_miles)} />
        <Fact label="Customer rate" value={money(load.customer_rate)} />
        {/* An ACTIVE load has no carrier rate. `—` is the honest rendering of
            that; `$0.00` would be a different claim entirely. */}
        <Fact label="Carrier rate" value={money(load.carrier_rate)} />
        {/* An unassigned load has no carrier at all, which is a different fact
            from a carrier whose TMS row carries no name. Both print as `—`
            here, so the id is shown alongside rather than instead. */}
        <Fact label="Carrier" value={party(load.carrier?.name ?? null, load.source_carrier_id)} />
        <Fact label="Carrier phone" value={load.carrier?.phone ?? UNKNOWN} />
        <Fact
          label="Customer"
          value={party(load.customer?.name ?? null, load.source_customer_id)}
        />
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

/** `Name (id)`, or the bare id, or `—` when the load names nobody. */
function party(name: string | null, sourceId: string | null): string {
  if (sourceId === null) return UNKNOWN;
  return name === null ? sourceId : `${name} (${sourceId})`;
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
            <th>Window</th>
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
                {/* Always the raw city/state/zip, resolved or not. A geo-null
                    stop is excluded from lane statistics, which is not the same
                    as hidden — but it must not read like a resolved one, so the
                    flag rides next to the value rather than in a footnote. */}
                {place(stop.location)}
                {stop.location.place === null ? (
                  <span
                    className="flag"
                    title="No row in the offline geo table for this city/state/zip"
                  >
                    not on the map — excluded from lane statistics
                  </span>
                ) : (
                  <span className="muted"> ({stop.location.place.metro})</span>
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
    </section>
  );
}

function stopKind(stop: Stop): string {
  if (stop.is_pickup && stop.is_drop) return "pickup + drop";
  if (stop.is_pickup) return "pickup";
  if (stop.is_drop) return "drop";
  return UNKNOWN;
}
