/**
 * U2 — the load list. Broker dropdown and status filter live in `App`; this
 * component owns the table.
 *
 * `ACTIVE` loads are the ones the whole platform exists to answer for, so they
 * are marked and given the call-to-action. They are *not* re-sorted to the top:
 * the API decides the order of its own list, and a UI that quietly reorders
 * results is a UI whose display can disagree with the thing that produced it.
 */

import type { Load, LoadStatus } from "./types";
import { listLoads } from "./api";
import { day, equipment, hasGeoNullLaneEnd, lane, laneEnds, money, UNKNOWN } from "./format";
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
    return <p className="note">Loading loads…</p>;
  }
  if (loads.state === "error") {
    return (
      <p className="error">
        Could not load the load list: {loads.message}
      </p>
    );
  }
  if (loads.data.length === 0) {
    return (
      <p className="note">
        No loads for this broker{status ? ` with status ${status}` : ""}.
      </p>
    );
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Load</th>
          <th>Lane</th>
          <th>Equipment</th>
          <th>Pickup</th>
          <th>Delivery</th>
          <th>Status</th>
          <th className="num">Customer rate</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {loads.data.map((load) => (
          <LoadRow key={load.source_load_id} load={load} onSelect={onSelect} />
        ))}
      </tbody>
    </table>
  );
}

function LoadRow({ load, onSelect }: { load: Load; onSelect: (loadId: string) => void }) {
  const { origin, destination } = laneEnds(load);
  const isActive = load.status === "ACTIVE";

  return (
    <tr className={isActive ? "active-load" : undefined}>
      <td>
        <button className="link" onClick={() => onSelect(load.source_load_id)}>
          {load.load_number ?? load.source_load_id}
        </button>
      </td>
      <td>
        {lane(load)}
        {/* Geo-null is excluded from lane stats but never hidden — say so here
            rather than letting the load look ordinary. */}
        {hasGeoNullLaneEnd(load) && (
          <span className="flag" title="Not matched in the geo table: excluded from lane statistics">
            no geo match
          </span>
        )}
      </td>
      <td>{equipment(load.equipment)}</td>
      <td>{day(origin?.scheduled_date ?? null)}</td>
      <td>{day(destination?.scheduled_date ?? null)}</td>
      <td>
        <span className={isActive ? "status status-active" : "status"}>{load.status}</span>
      </td>
      <td className="num">{money(load.customer_rate)}</td>
      <td>
        {isActive ? (
          <button className="link" onClick={() => onSelect(load.source_load_id)}>
            needs a carrier →
          </button>
        ) : (
          <span className="muted">{UNKNOWN}</span>
        )}
      </td>
    </tr>
  );
}
