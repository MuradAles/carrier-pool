/**
 * U5 — every carrier this broker has used, ranked, shown against each other.
 *
 * A score means nothing on its own, so the row is a comparison: one column per
 * signal, each drawn as a bar, with a dashed rule marking the best value any
 * candidate reached for that signal. A rep reads the gap without doing any
 * arithmetic, and without a number they would have to compare digit by digit.
 *
 * The main row carries only what a rep acts on: who, how to reach them, the
 * score, the shape of the signals, and the plain-English reasons the API
 * already wrote. The weight arithmetic — weight, normalized value, contribution
 * — is audit machinery aimed at a different reader, so it sits behind a
 * collapsible that is closed by default.
 *
 * Three things this panel deliberately does not do:
 *
 * * **It does not re-sort.** The API ranked by the unrounded weighted sum,
 *   tie-broken by carrier id (PRD section 8). `rank` is printed from the field,
 *   not from a counter, so a list that ever arrived out of order would show it
 *   rather than quietly renumbering itself.
 * * **It does not truncate or filter.** A carrier scoring near zero comes back
 *   last with an accurate reason, and hiding it defeats the point — a silently
 *   short list is indistinguishable from a bug.
 * * **It does not compute.** `score` was rounded once, server-side, half-up
 *   (DECISIONS.md D19); `contribution` is already weight × value. The reasons
 *   were generated from the same numbers that produced the score (invariant 2),
 *   so they are rendered verbatim and in order. Nothing here re-adds the
 *   signals, re-rounds the score, or turns a stored ratio into a percentage.
 */

import { getRecommendations } from "./api";
import {
  contribution as fmtContribution,
  equipmentFilter,
  score as fmtScore,
  signalValue,
  weight as fmtWeight,
} from "./format";
import { TierWalkLine } from "./TierWalk";
import type { CarrierScore, Recommendations, Signal } from "./types";
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
    <section className="panel">
      <div className="panel-hd">
        <h3>Carriers, ranked</h3>
        <span className="sub">
          {recommendations.state === "ready"
            ? `${recommendations.data.carriers.length} candidates · one column per signal, the bar is the score it earned`
            : "who to call, and why"}
        </span>
      </div>

      {recommendations.state !== "ready" ? (
        <p className={recommendations.state === "error" ? "state error" : "state"}>
          {recommendations.state === "loading" && "Loading carriers…"}
          {recommendations.state === "idle" && "No load selected."}
          {recommendations.state === "error" &&
            `Could not rank carriers: ${recommendations.message}`}
        </p>
      ) : (
        <Ranking ranking={recommendations.data} />
      )}
    </section>
  );
}

function Ranking({ ranking }: { ranking: Recommendations }) {
  const { carriers } = ranking;

  if (carriers.length === 0) {
    return (
      <>
        <p className="provenance" style={{ margin: "18px" }}>
          {ranking.basis}
        </p>
        <p className="caveat weak" style={{ margin: "0 18px 18px" }}>
          This broker has no carriers on record, so there is nobody to rank for this load.
        </p>
      </>
    );
  }

  // The column layout follows whatever signals the API scored, in its order,
  // with its labels — the browser does not know the signal list.
  const columns = carriers[0].signals;

  // The reference line in each cell: the best value any candidate reached for
  // that signal. A position, never a printed number, and the whole reason this
  // grid is readable — a bar means nothing without the field to compare it to.
  const best = new Map<string, number>();
  for (const carrier of carriers) {
    for (const signal of carrier.signals) {
      best.set(signal.name, Math.max(best.get(signal.name) ?? 0, signal.value));
    }
  }

  return (
    <>
      <p className="provenance" style={{ margin: "18px 18px 0" }}>
        {ranking.basis}
      </p>

      <div style={{ padding: "14px 18px 0" }}>
        <span className="chip mute">as of {ranking.as_of}</span>{" "}
        <span className="chip mute">{ranking.tier ?? "no tier accepted"}</span>{" "}
        <span className="chip mute">{ranking.lane_key ?? "no lane key"}</span>{" "}
        <span className="chip mute">{equipmentFilter(ranking.equipment_pool)}</span>{" "}
        {/* The count pair, not a percentage: `lane_on_time_rate` is a raw ratio
            and `basis` above already states it in words. Two renderings of one
            rate is exactly the disagreement invariant 2 forbids. */}
        <span className="chip mute">
          lane on-time {ranking.lane_on_time_count}/{ranking.lane_on_time_eligible_count}
        </span>
      </div>

      <TierWalkLine walk={ranking.walk} />

      <div className="cgrid-hd">
        <div className="h">Carrier</div>
        <div className="h">Score</div>
        <div className="sigcols">
          {columns.map((column) => (
            <div className="sh" key={column.name}>
              <span className="n">{column.label}</span>
            </div>
          ))}
        </div>
      </div>

      {carriers.map((scored, index) => (
        <CarrierRow
          key={scored.carrier.source_carrier_id}
          scored={scored}
          total={carriers.length}
          best={best}
          isTop={index === 0}
          isLast={index === carriers.length - 1 && carriers.length > 1}
        />
      ))}

      <div className="note">
        Order is exactly what the ranking API returned — <b>not re-sorted, filtered or trimmed
        in the browser</b>. The bottom carrier is shown with its reasons rather than dropped,
        because "why is nobody good for this lane" is a real answer. The dashed rule inside
        each cell marks the best value any candidate reached for that signal, so a gap is
        visible without arithmetic.
      </div>
    </>
  );
}

function CarrierRow({
  scored,
  total,
  best,
  isTop,
  isLast,
}: {
  scored: CarrierScore;
  total: number;
  best: Map<string, number>;
  isTop: boolean;
  isLast: boolean;
}) {
  const { carrier } = scored;

  // A reason is marked negative only when the signal it came from scored zero.
  // That is a fact off the payload, not a judgement made here — and the strings
  // are identical because both come from the same generated reason.
  const noCredit = new Set(
    scored.signals.filter((signal) => signal.value === 0).map((signal) => signal.reason),
  );

  return (
    <div className={`crow${isTop ? " top" : ""}${scored.score === 0 ? " zero" : ""}`}>
      <div className="cname">
        <div className="rk">
          {scored.rank} of {total}
          {isLast && (
            <span className="chip weak" style={{ marginLeft: "6px" }}>
              Weakest
            </span>
          )}
        </div>
        <div className="nm">{carrier.name ?? carrier.source_carrier_id}</div>
        <div className="meta mono">{carrier.phone ?? "no phone on file"}</div>
        <div className="meta">
          {carrier.mc_number !== null && <>MC {carrier.mc_number}</>}
          {carrier.mc_number !== null && carrier.dot_number !== null && (
            <span className="sep">·</span>
          )}
          {carrier.dot_number !== null && <>DOT {carrier.dot_number}</>}
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
        <div className="of">of 100</div>
        <div className="track">
          <i style={{ width: `${Math.max(0, Math.min(100, scored.score))}%` }} />
        </div>
      </div>

      <div className="cells">
        {scored.signals.map((signal) => (
          <SignalCell key={signal.name} signal={signal} best={best.get(signal.name) ?? 0} />
        ))}
      </div>

      {/* In the API's order, all of them. PRD section 8 orders these by signal
          with the non-scoring rate note last; re-sorting or cutting to the top
          three would hide the reason a carrier is ranked where it is. */}
      <ul className="creasons">
        {scored.reasons.map((reason, index) => (
          <li key={index} className={noCredit.has(reason) ? "neg" : undefined}>
            {reason}
          </li>
        ))}
      </ul>

      <ScoreCalculation scored={scored} />
    </div>
  );
}

function SignalCell({ signal, best }: { signal: Signal; best: number }) {
  const scored = signal.value > 0;
  return (
    <div className="cell">
      <span className="label">{signal.label}</span>
      <div className="box">
        {scored ? (
          <div className="f" style={{ height: `${Math.min(100, signal.value * 100)}%` }} />
        ) : (
          <div className="empty">none</div>
        )}
        {/* Drawn only where it is not the carrier's own bar top, so the leader
            does not get a redundant rule across its fill. */}
        {best > signal.value && (
          <div className="best" style={{ bottom: `calc(${Math.min(100, best * 100)}% - 1px)` }} />
        )}
      </div>
    </div>
  );
}

/**
 * The arithmetic, closed by default.
 *
 * A rep phoning carriers does not act on `0.35 × 0.615`; they act on "ran this
 * lane 8 times, truck 11.6 mi from your pickup", which is what the reasons
 * above already say. This is for the second reader — the one checking that the
 * published score is the sum of its parts — so the fact leads and the
 * normalized value follows it, never a bare decimal on its own.
 *
 * Every number in here is read from the payload. `contribution` is the API's
 * own weight × value, and the total is the published `score`; multiplying or
 * adding here would be a second source of truth for a number that exists.
 */
function ScoreCalculation({ scored }: { scored: CarrierScore }) {
  return (
    <details className="calc">
      <summary>How this score was calculated</summary>
      <table>
        <thead>
          <tr>
            <th>Signal</th>
            <th className="r">Weight</th>
            <th>What we observed, and what it normalizes to</th>
            <th className="r">Contribution</th>
          </tr>
        </thead>
        <tbody>
          {scored.signals.map((signal) => (
            <tr key={signal.name}>
              <td className="lbl">{signal.label}</td>
              <td className="r">{fmtWeight(signal.weight)}</td>
              <td>
                <span className="fact">{signal.reason}</span>
                <span className="norm">{signalValue(signal.value)}</span>
              </td>
              <td className="r">{fmtContribution(signal.contribution)}</td>
            </tr>
          ))}
          <tr className="sum">
            <td className="lbl" colSpan={3}>
              Score, rounded once by the API
            </td>
            <td className="r">{fmtScore(scored.score)}</td>
          </tr>
        </tbody>
      </table>
      <p className="foot">
        Each contribution is that signal's weight times its value, multiplied by the API rather
        than here. The contributions sum to the exact score, which the API rounds once, half-up,
        to the figure in the last row — so adding the printed two-decimal contributions by hand
        can differ from it in the final place.
      </p>
    </details>
  );
}
