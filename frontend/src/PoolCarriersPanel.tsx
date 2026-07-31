/**
 * S4 — the labeled second section: carriers this broker has never used, known
 * to other brokers that have opted into the shared pool (DECISIONS.md D17).
 *
 * Four things this panel is careful about:
 *
 * * **It labels every row.** The section header says it, and each row carries
 *   `source` from the payload. D4 wants a leak to be visible in the output
 *   rather than silent, and a pool row that ever turned up outside this panel
 *   would still be saying where it came from.
 * * **It does not present these scores as rivals to the ranking.** They are on
 *   the same 0-100 scale and they are lower bounds — every signal was scored at
 *   the weakest end of a band, and deadhead is structurally zero because truck
 *   position does not cross. The panel says so once, from `basis`, and the
 *   per-row reasons say it again line by line. It is drawn as a separate panel
 *   below the ranking, never interleaved with it.
 * * **It renders bands as bands.** `20-49` is printed as `20-49`. Turning a
 *   band into a midpoint would be the browser inventing a number, which is the
 *   one thing this frontend is not allowed to do — and it would be inventing
 *   the specific number the boundary exists to withhold.
 * * **It never computes.** Same rule as the ranking panel: `score` was rounded
 *   once, server-side, and the reasons were generated from the same values.
 */

import { useState } from "react";

import { getPoolCarriers, getPoolOptIn, setPoolOptIn } from "./api";
import { equipment, equipmentFilter, score as fmtScore, UNKNOWN } from "./format";
import type { PoolCarrierScore, PoolOptIn, PoolSection } from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
}

export function PoolCarriersPanel({ brokerId, loadId }: Props) {
  // The one piece of local state: bumped after a successful opt-in write so
  // both fetches below re-run. A router or a store would be the same mistake
  // here as turning three TMS adapters into a plugin framework.
  const [generation, setGeneration] = useState(0);

  const optIn = useApi<PoolOptIn>(`${brokerId}|${generation}`, (signal) =>
    getPoolOptIn(brokerId, signal),
  );
  const pool = useApi<PoolSection>(`${brokerId}|${loadId}|${generation}`, (signal) =>
    getPoolCarriers(brokerId, loadId, signal),
  );

  return (
    <section className="panel">
      <div className="panel-hd">
        <h3>Shared carrier pool</h3>
        <span className="sub">
          carriers you have never used · scored from bands, so these are lower bounds and are
          not comparable with the ranking above
        </span>
        <span className="spacer" />
        {optIn.state === "ready" && (
          <OptInControl
            optedIn={optIn.data.opted_in}
            brokerId={brokerId}
            onChanged={() => setGeneration((n) => n + 1)}
          />
        )}
      </div>

      {optIn.state === "loading" && <p className="state">Checking pool membership…</p>}
      {optIn.state === "idle" && <p className="state">No broker selected.</p>}
      {optIn.state === "error" && (
        <p className="state error">Could not read the pool opt-in: {optIn.message}</p>
      )}

      {pool.state === "loading" && <p className="state">Loading pool carriers…</p>}
      {pool.state === "idle" && <p className="state">No load selected.</p>}
      {pool.state === "error" && (
        <p className="state error">Could not read the shared pool: {pool.message}</p>
      )}
      {pool.state === "ready" && <Section section={pool.data} />}
    </section>
  );
}

/**
 * Join or leave. The state it reports comes back from the server, never from
 * optimistic local state, so what the control says is what the database holds.
 */
function OptInControl({
  optedIn,
  brokerId,
  onChanged,
}: {
  optedIn: boolean;
  brokerId: string;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function toggle() {
    setBusy(true);
    setFailure(null);
    try {
      await setPoolOptIn(brokerId, !optedIn, new AbortController().signal);
      onChanged();
    } catch (error) {
      setFailure(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="sub">
      <span className={optedIn ? "chip good" : "chip mute"}>
        {optedIn ? "In the pool" : "Not in the pool"}
      </span>{" "}
      <button className="link" onClick={toggle} disabled={busy}>
        {busy ? "saving…" : optedIn ? "leave the pool" : "join the pool"}
      </button>
      {failure !== null && <span className="error"> {failure}</span>}
    </span>
  );
}

function Section({ section }: { section: PoolSection }) {
  return (
    <>
      {/* The finished sentence from the API, whichever case it describes: not
          opted in, not an ACTIVE load, no pool lane, or a real answer. */}
      <p className="provenance" style={{ margin: "18px" }}>
        {section.basis}
      </p>

      <div style={{ padding: "0 18px 14px" }}>
        {/* `opted_in` and `eligible` fail for different reasons, so they are
            reported separately rather than collapsed into one "no".

            `eligible` is a precondition, not an outcome: it says this load may
            be asked about, never that the pool found anyone. Labelling it
            "pool answered" put a green yes directly above "no pool lane for
            this load", which is a chip disagreeing with the sentence under it.
            The field is named for what it holds. */}
        <span className={section.opted_in ? "chip good" : "chip mute"}>
          opted in: {section.opted_in ? "yes" : "no"}
        </span>{" "}
        <span className={section.eligible ? "chip good" : "chip mute"}>
          load eligible: {section.eligible ? "yes" : "no"}
        </span>{" "}
        <span className="chip mute">as of {section.as_of}</span>{" "}
        <span className="chip mute">{section.tier ?? "no pool tier"}</span>{" "}
        <span className="chip mute">{section.lane_key ?? "no pool lane"}</span>{" "}
        <span className="chip mute">
          {section.equipment_pool === null
            ? UNKNOWN
            : equipmentFilter(section.equipment_pool)}
        </span>
      </div>

      {section.carriers.map((scored) => (
        <PoolRow
          key={scored.carrier.mc_number}
          scored={scored}
          total={section.carriers.length}
        />
      ))}
    </>
  );
}

function PoolRow({ scored, total }: { scored: PoolCarrierScore; total: number }) {
  const { carrier } = scored;
  return (
    <div className="poolrow">
      <div className="cname">
        <div className="rk">
          {scored.rank} of {total}{" "}
          {/* Per-row, not just per-section. A leak should say so wherever it lands. */}
          <span
            className="chip mute"
            title="Known through the opt-in shared carrier pool, not from your own loads"
          >
            {scored.source}
          </span>
        </div>
        <div className="nm">{carrier.name ?? `MC ${carrier.mc_number}`}</div>
        <div className="meta mono">{carrier.phone ?? "no phone on file"}</div>
        <div className="meta">
          MC {carrier.mc_number}
          {carrier.dot_number !== null && (
            <>
              <span className="sep">·</span>DOT {carrier.dot_number}
            </>
          )}
          {carrier.home_city !== null && (
            <>
              <span className="sep">·</span>
              {carrier.home_city}
              {carrier.home_state === null ? "" : `, ${carrier.home_state}`}
            </>
          )}
        </div>
      </div>

      <div className="cscore">
        <div className="v">{fmtScore(scored.score)}</div>
        <div className="of">lower bound</div>
        <div className="track">
          <i style={{ width: `${Math.max(0, Math.min(100, scored.score))}%` }} />
        </div>
      </div>

      <div>
        {/* Bands printed as bands, and the null on-time band printed as "not
            delivered yet" rather than as a percentage nobody sent. */}
        <div className="bands">
          <span className="band">
            <b>loads</b>
            {carrier.load_band}
          </span>
          <span className="band">
            <b>on-time</b>
            {carrier.on_time_band === null ? "none delivered yet" : carrier.on_time_band}
          </span>
          <span className="band">
            <b>activity</b>
            {carrier.active_recently ? "active recently" : "not active recently"}
          </span>
          <span className="band">
            <b>runs</b>
            {carrier.equipment_operated.length === 0
              ? UNKNOWN
              : carrier.equipment_operated.map(equipment).join(", ")}
          </span>
          <span className="band">
            <b>brokers</b>
            {carrier.contributor_count}
          </span>
        </div>
        <ul className="creasons" style={{ borderTop: 0, margin: 0, padding: 0 }}>
          {scored.reasons.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}
