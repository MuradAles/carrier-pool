/**
 * U2 — the load list. Broker dropdown and status filter live in `App`; this
 * component owns the table.
 *
 * Dense and tabular on purpose: a coverage rep reads this fifty times a day and
 * compares rows against each other, so every figure is on screen without a
 * click, money and distance are right-aligned, and every digit is tabular.
 *
 * `ACTIVE` loads are the ones the whole platform exists to answer for, so they
 * are marked. They are *not* re-sorted to the top: the API decides the order of
 * its own list, and a UI that quietly reorders results is a UI whose display
 * can disagree with the thing that produced it.
 */

import type { Load, LoadStatus, Stop } from "./types";
import { listLoads } from "./api";
import {
  day,
  equipment,
  hasGeoNullLaneEnd,
  laneEnds,
  miles,
  money,
  pounds,
  town,
  UNKNOWN,
} from "./format";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  status: LoadStatus | null;
  onSelect: (loadId: string) => void;
}

export function LoadList({ brokerId, status, onSelect }: Props) {
  const loads = useApi<Load[]>(`${brokerId}|${status ?? "ALL"}`, (signal) =>
    listLoads(brokerId, status, signal),
  );

  if (loads.state === "idle" || loads.state === "loading") {
    return <p className="state">Loading loads…</p>;
  }
  if (loads.state === "error") {
    return <p className="state error">Could not load the load list: {loads.message}</p>;
  }

  const rows = loads.data;

  // The distance bar is a comparison inside this table, so it is scaled to the
  // longest load in view. A layout width, never a printed number — the miles
  // beside it are `distance_miles` exactly as stored.
  const longest = rows.reduce((best, load) => Math.max(best, load.distance_miles ?? 0), 0);

  return (
    <>
      <div className="screen-label">
        <h2>Loads needing coverage</h2>
        <span className="sub">
          {rows.length} load{rows.length === 1 ? "" : "s"}
          {status === null ? ", all statuses" : ` with status ${status}`}
        </span>
      </div>

      <div className="panel">
        {rows.length === 0 ? (
          <p className="state">
            No loads for this broker{status ? ` with status ${status}` : ""}.
          </p>
        ) : (
          <>
            <div className="tablewrap">
              <table className="loads">
                <thead>
                  <tr>
                    <th style={{ width: "116px" }}>Load</th>
                    <th className="lane-cell">Lane</th>
                    <th style={{ width: "84px" }}>Equip</th>
                    <th className="r" style={{ width: "154px" }}>
                      Distance
                    </th>
                    <th className="r" style={{ width: "94px" }}>
                      Weight
                    </th>
                    <th style={{ width: "106px" }}>Pickup</th>
                    <th style={{ width: "106px" }}>Delivery</th>
                    <th className="r" style={{ width: "104px" }}>
                      Customer
                    </th>
                    <th className="r" style={{ width: "136px" }}>
                      Carrier rate
                    </th>
                    <th style={{ width: "92px" }}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((load) => (
                    <LoadRow
                      key={load.source_load_id}
                      load={load}
                      longest={longest}
                      onSelect={onSelect}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="note">
              Distance bars are scaled to the longest load in view. <b>Carrier rate is empty
              on an ACTIVE load</b> — it is not covered yet, so there is no number, not a zero.
              Rows are in the order the API returned them.
            </div>
          </>
        )}
      </div>
    </>
  );
}

function LoadRow({
  load,
  longest,
  onSelect,
}: {
  load: Load;
  longest: number;
  onSelect: (loadId: string) => void;
}) {
  const { origin, destination } = laneEnds(load);
  const isActive = load.status === "ACTIVE";

  return (
    <tr>
      <td>
        <button className="rowlink" onClick={() => onSelect(load.source_load_id)}>
          {load.load_number ?? load.source_load_id}
        </button>
      </td>
      <td className="lane-cell">
        <div className="lane-txt">
          {origin ? town(origin.location) : UNKNOWN}
          <span className="arrow">→</span>
          {destination ? town(destination.location) : UNKNOWN}
          {/* Geo-null is excluded from lane stats but never hidden — say so
              here rather than letting the load look ordinary. */}
          {hasGeoNullLaneEnd(load) && (
            <span
              className="flag"
              title="Not matched in the offline geo table: excluded from lane statistics, still shown"
            >
              no geo match
            </span>
          )}
        </div>
        <div className="lane-sub mono">{laneSub(origin, destination)}</div>
      </td>
      <td>{equipment(load.equipment)}</td>
      <td className="r">
        {load.distance_miles === null ? (
          <span className="dash">{UNKNOWN}</span>
        ) : (
          <div className="milesbar">
            <span className="tnum">{miles(load.distance_miles)}</span>
            <span className="track">
              <i
                className="fill"
                style={{ width: `${longest > 0 ? (load.distance_miles / longest) * 100 : 0}%` }}
              />
            </span>
          </div>
        )}
      </td>
      <td className="r tnum">{pounds(load.weight_lbs)}</td>
      <td className="tnum">{day(origin?.scheduled_date ?? null)}</td>
      <td className="tnum">{day(destination?.scheduled_date ?? null)}</td>
      <td className="r tnum">{money(load.customer_rate)}</td>
      {/* An ACTIVE load has no carrier rate. `—` with the reason beside it is
          the honest rendering; `$0.00` would be a different claim entirely. */}
      <td className="r tnum">
        {load.carrier_rate === null ? (
          <span className="dash">
            {UNKNOWN} <small>{isActive ? "not yet known" : "none recorded"}</small>
          </span>
        ) : (
          money(load.carrier_rate)
        )}
      </td>
      <td>
        <span className={isActive ? "chip active" : "chip mute"}>{load.status}</span>
      </td>
    </tr>
  );
}

/**
 * The zips and metros under the lane, as the payload states them.
 *
 * Deliberately not the zip3 lane key: `Place` carries no zip3 field, and
 * slicing one out of the zip here would be the browser minting a lane key the
 * API never sent. The metro pair is a real field, so that is what is shown.
 */
function laneSub(origin: Stop | null, destination: Stop | null): string {
  const zips = `${origin?.location.zip ?? UNKNOWN} → ${destination?.location.zip ?? UNKNOWN}`;
  const from = origin?.location.place?.metro;
  const to = destination?.location.place?.metro;
  if (!from && !to) return zips;
  return `${zips} · ${from ?? UNKNOWN} → ${to ?? UNKNOWN}`;
}
