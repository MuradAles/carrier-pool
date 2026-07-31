/**
 * U4 — what this load should cost, and everything that backs the number.
 *
 * The estimate and the lane definition that produced it are one answer, so this
 * module renders both panels side by side from a single fetch: if the price
 * call fails there is no walk to draw either, and two panels disagreeing about
 * which rung won would be worse than one panel missing.
 *
 * Every figure here is a field of the response. The browser multiplies nothing:
 * `point_usd` is already median-rate × miles, `low_usd`/`high_usd` are already
 * the p25/p75 ends, and `confidence` is already the domain's label. Recomputing
 * any of them would be a second source of truth for a number that exists, which
 * CLAUDE.md invariant 2 calls the worst possible bug in this project. There is
 * deliberately no margin figure: the API does not return one, and customer rate
 * minus estimate is exactly the kind of arithmetic that belongs upstream.
 *
 * The chart puts the estimate, the middle half of the comparable loads, and
 * what the customer pays on one dollar scale, because a price means nothing
 * without the spread around it. Pixel positions are computed; every *label* is
 * a value the API sent.
 *
 * Rates per mile print at four decimals, their stored precision. DECISIONS.md
 * D18 requires an estimate to be reproducible by hand from its own evidence,
 * and `1.81 × 296.0` is not the `$534.28` printed above it — `1.8050 × 296.0` is.
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
} from "./format";
import { TierWalkFunnel } from "./TierWalk";
import type { PriceEstimate } from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
  /** The load's own `customer_rate`, plotted beside the estimate. May be null. */
  customerRate: number | null;
}

export function PriceEstimatePanel({ brokerId, loadId, customerRate }: Props) {
  const estimate = useApi<PriceEstimate>(`${brokerId}|${loadId}`, (signal) =>
    getPriceEstimate(brokerId, loadId, signal),
  );

  if (estimate.state !== "ready") {
    return (
      <section className="panel">
        <div className="panel-hd">
          <h3>What this load should cost</h3>
        </div>
        <p className={estimate.state === "error" ? "state error" : "state"}>
          {estimate.state === "loading" && "Loading estimate…"}
          {estimate.state === "idle" && "No load selected."}
          {estimate.state === "error" && `Could not price this load: ${estimate.message}`}
        </p>
      </section>
    );
  }

  const data = estimate.data;

  return (
    <div className="cols">
      <section className="panel">
        <div className="panel-hd">
          <h3>What this load should cost</h3>
          <span className="sub">estimate and the spread behind it</span>
          <span className="spacer" />
          {/* Invariant 6: low confidence is labelled, never quietly styled the
              same as high. The class carries the level as well as the word. */}
          <span className={`chip ${confidenceChip(data)}`}>{confidence(data.confidence)}</span>
        </div>

        <div className="panel-bd">
          <p className="headline">
            {/* `null` is not `$0`. A load with no distance, or a broker with no
                history at any rung, has no estimate — `provenance` says which. */}
            <span className={data.point_usd === null ? "point dash" : "point"}>
              {money(data.point_usd)}
            </span>
            <span className="of">
              {data.point_usd === null
                ? "no estimate for this load"
                : `median of ${loads(data.load_count)}`}
            </span>
          </p>

          <DollarScale estimate={data} customerRate={customerRate} />

          {/* Printed at stored precision so the dollars above can be checked
              by hand: p50 × distance is the point estimate, to the cent
              (DECISIONS.md D18). The multiplication is the API's; the two rows
              are here so a rep can verify it, not so the browser can do it. */}
          <table className="grid" style={{ marginTop: "18px" }}>
            <thead>
              <tr>
                <th />
                <th className="r">p25</th>
                <th className="r">p50 (median)</th>
                <th className="r">p75</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="muted">Rate per mile, as paid on this lane</td>
                <td className="r">{ratePerMile(data.rate_per_mile_p25)}</td>
                <td className="r">{ratePerMile(data.rate_per_mile_p50)}</td>
                <td className="r">{ratePerMile(data.rate_per_mile_p75)}</td>
              </tr>
              <tr>
                <td className="muted">× {miles(data.distance_miles)}</td>
                <td className="r">{money(data.low_usd)}</td>
                <td className="r">{money(data.point_usd)}</td>
                <td className="r">{money(data.high_usd)}</td>
              </tr>
            </tbody>
          </table>

          <p className="provenance">{data.provenance}</p>

          {/* D15: a pool spanning trailer types has a p75 no carrier was ever
              paid, so confidence is capped at medium and the mix is named
              rather than summarized away. The counts are the API's. */}
          {data.is_heterogeneous && (
            <div className="caveat">
              <p>
                Mixed equipment pool — the loads behind this estimate are not all the same
                trailer type, so these rates are not like-for-like. This load is{" "}
                {equipment(data.load_equipment).toLowerCase()}; the pool is{" "}
                {equipmentFilter(data.equipment_filter).toLowerCase()}:
              </p>
              <ul>
                {data.equipment_mix.map((line) => (
                  <li key={line.equipment}>
                    {equipment(line.equipment)} — {loads(line.load_count)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <div className="note">
          Evidence spans <b>{day(data.first_load_date)}</b> to <b>{day(data.last_load_date)}</b>.
          Every figure above is returned by the price API — including the margin against the
          customer rate, which is <b>not</b> shown, because the API does not compute one and the
          browser is not allowed to.
        </div>
      </section>

      <section className="panel">
        <div className="panel-hd">
          <h3>How the lane was defined</h3>
          <span className="sub">every rung, including the ones not needed</span>
        </div>
        <TierWalkFunnel walk={data.walk} />
      </section>
    </div>
  );
}

function confidenceChip(estimate: PriceEstimate): string {
  if (estimate.confidence === "high") return "good";
  if (estimate.confidence === "medium") return "warn";
  return "weak";
}

/* ------------------------------------------------------------------------- *
 * The dollar scale.
 *
 * One axis, three marks: the p25–p75 band, the median estimate on it, and what
 * the customer pays. A rep reads the gap between the last two without being
 * handed a computed margin.
 * ------------------------------------------------------------------------- */

const W = 900;
const H = 200;
const LEFT = 62;
const RIGHT = 862;
const AXIS_Y = 118;
/** Data callouts sit between the axis and its tick labels, never on top of them. */
const CALLOUT_Y = 142;
const TICK_LABEL_Y = 181;

function DollarScale({
  estimate,
  customerRate,
}: {
  estimate: PriceEstimate;
  customerRate: number | null;
}) {
  const { low_usd: low, point_usd: point, high_usd: high } = estimate;

  if (point === null) {
    return (
      <p className="caveat weak">
        There is no dollar estimate to plot for this load — see the provenance line below for
        which of "no lane evidence" or "no usable distance" applies.
      </p>
    );
  }

  const marks = [low, point, high, customerRate].filter((v): v is number => v !== null);
  const lo = Math.min(...marks);
  const hi = Math.max(...marks);
  const pad = (hi - lo || Math.max(1, point * 0.1)) * 0.18;
  const min = lo - pad;
  const max = hi + pad;
  const x = (value: number) => LEFT + ((value - min) / (max - min)) * (RIGHT - LEFT);

  const ticks = axisTicks(min, max);

  return (
    <>
      <svg
        className="chart"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={
          `Dollar scale. Estimate ${money(point)}` +
          (low !== null && high !== null ? `, middle half ${money(low)} to ${money(high)}` : "") +
          (customerRate !== null ? `. Customer pays ${money(customerRate)}` : "")
        }
      >
        {ticks.map((t) => (
          <line key={`g${t}`} className="grid-line" x1={x(t)} y1={26} x2={x(t)} y2={AXIS_Y} />
        ))}

        {/* p25 – p75: the middle half of the comparable loads. */}
        {low !== null && high !== null && (
          <>
            <rect
              x={x(low)}
              y={66}
              width={Math.max(1, x(high) - x(low))}
              height={36}
              fill="var(--accent-sf)"
              stroke="var(--accent-2)"
              strokeWidth="1"
            />
            <text className="ax-label tnum" x={x(low)} y={60} textAnchor="middle">
              {money(low)}
            </text>
            <text className="ax-label tnum" x={x(high)} y={60} textAnchor="middle">
              {money(high)}
            </text>
          </>
        )}

        {/* The estimate itself. */}
        <line x1={x(point)} y1={58} x2={x(point)} y2={110} stroke="var(--accent)" strokeWidth="3" />
        <circle cx={x(point)} cy={84} r="5.5" fill="var(--accent)" />
        <text
          className="val-label"
          x={x(point)}
          y={CALLOUT_Y}
          textAnchor="middle"
          fontSize="16"
          fontWeight="640"
        >
          {money(point)}
        </text>
        <text className="key-label" x={x(point)} y={CALLOUT_Y + 17} textAnchor="middle">
          estimate (median)
        </text>

        {/* What the customer pays. No margin annotation: the API does not
            return one, and subtracting here would invent a second answer. */}
        {customerRate !== null && (
          <>
            <line
              x1={x(customerRate)}
              y1={58}
              x2={x(customerRate)}
              y2={110}
              stroke="var(--ink-2)"
              strokeWidth="2.5"
            />
            <polygon
              points={`${x(customerRate)},54 ${x(customerRate) - 5.5},45 ${x(customerRate) + 5.5},45`}
              fill="var(--ink-2)"
            />
            <text
              className="val-label"
              x={x(customerRate)}
              y={CALLOUT_Y}
              textAnchor="middle"
              fontSize="16"
              fontWeight="640"
            >
              {money(customerRate)}
            </text>
            <text className="key-label" x={x(customerRate)} y={CALLOUT_Y + 17} textAnchor="middle">
              customer pays
            </text>
          </>
        )}

        <line className="ax-line" x1={LEFT - 10} y1={AXIS_Y} x2={RIGHT + 10} y2={AXIS_Y} />
        <g className="ax-label tnum" textAnchor="middle">
          {ticks.map((t) => (
            <g key={`t${t}`}>
              <line className="ax-tick" x1={x(t)} y1={AXIS_Y} x2={x(t)} y2={AXIS_Y + 5} />
              <text x={x(t)} y={TICK_LABEL_Y}>
                {t}
              </text>
            </g>
          ))}
        </g>
        <text className="ax-label" x={LEFT - 10} y={H - 4}>
          US dollars, total linehaul
        </text>
      </svg>

      <div className="legend">
        <span>
          <i style={{ background: "var(--accent)" }} />
          estimate
        </span>
        <span>
          <i style={{ background: "var(--accent-sf)", border: "1px solid var(--accent-2)" }} />
          p25 – p75
        </span>
        {customerRate !== null && (
          <span>
            <i style={{ background: "var(--ink-2)" }} />
            customer rate
          </span>
        )}
      </div>
    </>
  );
}

/**
 * Round tick positions for the axis.
 *
 * These are scale annotations, not facts about the load: they say where $560 is
 * on the line, the way the ruler markings on a ruler do. Every figure that
 * makes a claim about this load — the estimate, the band ends, the customer
 * rate — is printed from the payload and appears above, not here.
 */
function axisTicks(min: number, max: number, target = 8): number[] {
  const span = max - min;
  if (!(span > 0)) return [];
  const magnitude = Math.pow(10, Math.floor(Math.log10(span / target)));
  const normalized = span / target / magnitude;
  const step = (normalized >= 5 ? 10 : normalized >= 2 ? 5 : normalized >= 1 ? 2 : 1) * magnitude;
  const out: number[] = [];
  for (let value = Math.ceil(min / step) * step; value <= max; value += step) {
    out.push(Math.round(value * 1000) / 1000);
  }
  return out;
}
