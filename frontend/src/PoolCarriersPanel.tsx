/**
 * S4 — the labeled second section: carriers this broker has never used, known
 * to other brokers that have opted into the shared pool (DECISIONS.md D17).
 *
 * Four things this panel is careful about:
 *
 * * **It labels every row.** The section header says it, and each card carries
 *   `source` from the payload. D4 wants a leak to be visible in the output
 *   rather than silent, and a pool row that ever turned up outside this panel
 *   would still be saying where it came from.
 * * **It does not present these scores as rivals to the ranking.** They are on
 *   the same 0-100 scale and they are lower bounds — every signal was scored at
 *   the weakest end of a band, and deadhead is structurally zero because truck
 *   position does not cross. The panel says so once, from `basis`, and the
 *   per-card reasons say it again line by line.
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
    <section>
      <h3>Shared carrier pool</h3>

      {optIn.state === "loading" && <p className="note">Checking pool membership…</p>}
      {optIn.state === "idle" && <p className="note">No broker selected.</p>}
      {optIn.state === "error" && (
        <p className="error">Could not read the pool opt-in: {optIn.message}</p>
      )}
      {optIn.state === "ready" && (
        <OptInControl
          optedIn={optIn.data.opted_in}
          brokerId={brokerId}
          onChanged={() => setGeneration((n) => n + 1)}
        />
      )}

      {pool.state === "loading" && <p className="note">Loading pool carriers…</p>}
      {pool.state === "idle" && <p className="note">No load selected.</p>}
      {pool.state === "error" && (
        <p className="error">Could not read the shared pool: {pool.message}</p>
      )}
      {pool.state === "ready" && <Section section={pool.data} />}
    </section>
  );
}

/**
 * Join or leave. The write is deliberately blunt and unstyled — the state it
 * reports comes back from the server, never from optimistic local state, so
 * what the button says is what the database holds.
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
    <p className="contact">
      {optedIn ? "In the shared pool." : "Not in the shared pool (the default)."}{" "}
      <button className="link" onClick={toggle} disabled={busy}>
        {busy ? "saving…" : optedIn ? "leave the pool" : "join the pool"}
      </button>
      {failure !== null && <span className="error"> {failure}</span>}
    </p>
  );
}

function Section({ section }: { section: PoolSection }) {
  return (
    <>
      {/* The finished sentence from the API, whichever case it describes:
          not opted in, not an ACTIVE load, no pool lane, or a real answer. */}
      <p className="provenance">{section.basis}</p>

      <dl className="facts">
        <Fact label="As of" value={section.as_of} />
        <Fact label="In the pool" value={section.opted_in ? "yes" : "no"} />
        <Fact label="Pool answered" value={section.eligible ? "yes" : "no"} />
        <Fact label="Pool tier" value={section.tier ?? UNKNOWN} />
        <Fact label="Pool lane" value={section.lane_key ?? UNKNOWN} />
        <Fact
          label="Equipment pool"
          value={
            section.equipment_pool === null
              ? UNKNOWN
              : equipmentFilter(section.equipment_pool)
          }
        />
        <Fact label="Pool carriers" value={String(section.carriers.length)} />
      </dl>

      {section.carriers.length > 0 && (
        <div className="carriers">
          {section.carriers.map((carrier) => (
            <PoolCard key={carrier.carrier.mc_number} scored={carrier} />
          ))}
        </div>
      )}
    </>
  );
}

function PoolCard({ scored }: { scored: PoolCarrierScore }) {
  const { carrier } = scored;
  return (
    <article className="carrier">
      <h4>
        <span className="rank">#{scored.rank}</span> {carrier.name ?? `MC ${carrier.mc_number}`}{" "}
        <span className="score">{fmtScore(scored.score)}</span>{" "}
        {/* Per-row, not just per-section. A leak should say so wherever it lands. */}
        <span className="flag" title="Known through the opt-in shared carrier pool, not from your own loads">
          {scored.source}
        </span>
      </h4>
      <p className="contact">
        {carrier.phone ?? "no phone on file"} · MC {carrier.mc_number}
        {carrier.dot_number !== null && <> · DOT {carrier.dot_number}</>}
        {carrier.home_city !== null && (
          <>
            {" "}
            · based in {carrier.home_city}
            {carrier.home_state === null ? "" : `, ${carrier.home_state}`}
          </>
        )}
      </p>
      <ul className="reasons">
        {scored.reasons.map((reason, index) => (
          <li key={index}>{reason}</li>
        ))}
      </ul>
      {/* Bands printed as bands, and the null on-time band printed as "not
          delivered yet" rather than as a percentage nobody sent. */}
      <p className="muted evidence">
        Loads {carrier.load_band} · on-time{" "}
        {carrier.on_time_band === null ? "none delivered yet" : carrier.on_time_band} ·{" "}
        {carrier.active_recently ? "active recently" : "not active recently"} · runs{" "}
        {carrier.equipment_operated.length === 0
          ? UNKNOWN
          : carrier.equipment_operated.map(equipment).join(", ")}{" "}
        · known to {carrier.contributor_count} other broker
        {carrier.contributor_count === 1 ? "" : "s"}
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
