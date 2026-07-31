/**
 * The tier walk, rendered as the funnel it is.
 *
 * Invariant 6 says every lane answer reports which tier it used and how many
 * loads backed it. Reporting only the *winning* rung satisfies the letter of
 * that and not the point: "ZIP3, 12 loads" and "METRO, 31 loads because ZIP3
 * had 2" are different claims, and only the second lets a broker judge the
 * answer. So every rung the walk tried is drawn, with its count against the
 * minimum, and the verdict the domain wrote for it.
 *
 * `verdict` is printed, never rebuilt. The backend already has the sentence
 * ("ACCEPTED", "rejected, 2 < 5", "skipped, lane end not on the map"), and a
 * second construction of it from `load_count` and `accepted` here is a second
 * thing to get wrong.
 *
 * Rungs *after* the accepted one are absent from `rungs` because they were
 * never asked. They are still drawn — greyed, with no count and no bar — so the
 * ladder reads as a ladder and "we stopped here" is visible. That is structure,
 * not evidence: a never-queried rung shows `—`, never a zero.
 */

import { equipmentFilter, loads } from "./format";
import type { LaneTier, TierAttempt, TierWalk as Walk } from "./types";

/**
 * How wide the lane definition gets at each rung, as a schematic. Fixed
 * proportions, not measured areas — the only claim being made is "narrower is
 * better, and the accepted rung is the tightest one with enough history".
 */
const SCOPE_WIDTH: Record<LaneTier, number> = {
  ZIP3: 26,
  METRO: 62,
  REGION: 100,
  REGION_ANY: 100,
};

const TIER_DEF: Record<LaneTier, string> = {
  ZIP3: "3-digit zip pair",
  METRO: "metro pair",
  REGION: "region pair",
  REGION_ANY: "region pair, equipment filter dropped",
};

/**
 * The ladder, narrow to wide.
 *
 * `REGION_ANY` only exists as a rung when there was an equipment filter to
 * drop, so a load whose own equipment is `UNKNOWN` (`walk.equipment === "ANY"`)
 * has a three-rung ladder and is not shown a fourth it could never reach.
 */
function ladder(walk: Walk): LaneTier[] {
  const base: LaneTier[] = ["ZIP3", "METRO", "REGION"];
  return walk.equipment === "ANY" ? base : [...base, "REGION_ANY"];
}

export function TierWalkFunnel({ walk }: { walk: Walk }) {
  const asked = new Map(walk.rungs.map((rung) => [rung.tier, rung]));

  // The count bar is a comparison between rungs, so it is scaled to the biggest
  // count in this walk — with a floor of 20 so a walk of small counts does not
  // make 6 loads look like a landslide. A layout width, never a printed number.
  const scale = Math.max(20, ...walk.rungs.map((rung) => rung.load_count));

  return (
    <>
      <div className="panel-bd" style={{ paddingTop: "6px" }}>
        {ladder(walk).map((tier, index) => {
          const rung = asked.get(tier);
          return rung === undefined ? (
            // Only the first unasked rung explains itself; repeating the same
            // sentence three times down the ladder is noise, not evidence.
            <NotQueried key={tier} tier={tier} explain={index === walk.rungs.length} />
          ) : (
            <Rung key={tier} rung={rung} scale={scale} minSample={walk.min_sample} />
          );
        })}

        <div className="thr-key">
          <svg width="14" height="12" aria-hidden="true" style={{ verticalAlign: "-1px" }}>
            <line x1="7" y1="0" x2="7" y2="12" stroke="var(--warn)" strokeWidth="2" strokeDasharray="3 3" />
          </svg>{" "}
          dashed line = the {walk.min_sample}-load minimum needed to accept a rung · bars scaled
          0 – {scale} loads
        </div>

        <p className="rung-note" style={{ marginTop: "14px", borderTop: "1px solid var(--rule)", paddingTop: "12px" }}>
          The thin bar above each rung shows how wide the lane definition gets. Narrower is
          better: the accepted rung is the tightest one with enough history.
        </p>
      </div>

      {walk.tier === null ? (
        <p className="caveat weak" style={{ margin: "0 18px 18px" }}>
          No rung cleared the {walk.min_sample}-load minimum, so this answer has no lane
          evidence behind it.
        </p>
      ) : (
        <div className="note">
          Accepted <b>{walk.tier}</b> · {loads(walk.load_count)} ·{" "}
          {equipmentFilter(walk.equipment_filter)}.
          {/* Two different routes to an unfiltered pool, and a reader needs to
              know which one this was: the load's own equipment was never known,
              or the filtered rungs were all too thin and the walk gave up. */}
          {!walk.equipment_filtered && (
            <>
              {" "}
              {walk.equipment === "ANY"
                ? "This load's equipment is UNKNOWN, so no rung applied an equipment filter."
                : `No ${equipmentFilter(walk.equipment).toLowerCase()} rung cleared the minimum, so the accepted rung dropped the equipment filter.`}{" "}
              These rates mix trailer types and are not like-for-like.
            </>
          )}
        </div>
      )}
    </>
  );
}

function Rung({
  rung,
  scale,
  minSample,
}: {
  rung: TierAttempt;
  scale: number;
  minSample: number;
}) {
  const state = rung.accepted ? "on" : rung.skipped ? "off" : "rejected";
  return (
    <div className={`rung ${state}`}>
      <div className="rung-hd">
        <span className="rung-name mono">{rung.tier}</span>
        {/* A skipped rung has no key at all: the load's lane end is not on the
            map, so the question could not be formed. That is not the same as
            asking and finding nothing. */}
        <span className="rung-def">
          {rung.lane_key === null ? TIER_DEF[rung.tier] : rung.lane_key} ·{" "}
          {equipmentFilter(rung.equipment).toLowerCase()}
        </span>
        <span className="spacer" />
        {/* The domain's own finished sentence — "ACCEPTED", "rejected, 2 < 5",
            "skipped, lane end not on the map". Printed verbatim and exactly
            once: rebuilding it from `load_count` and `accepted` would be a
            second way to say the same thing and a second way to get it wrong. */}
        <span
          className={`chip verdict ${
            rung.accepted ? "good" : rung.skipped ? "warn" : "weak"
          }`}
        >
          {rung.verdict}
        </span>
      </div>

      <div className="scope">
        <i style={{ width: `${SCOPE_WIDTH[rung.tier]}%` }} />
      </div>

      <CountBar
        count={rung.skipped ? null : rung.load_count}
        scale={scale}
        minSample={minSample}
      />
    </div>
  );
}

/**
 * How many loads this rung found, against the minimum needed to accept it.
 *
 * The label sits inside the fill when there is room and clears both the fill
 * and the threshold marker when there is not — a count printed underneath a
 * dashed line is a count a rep has to squint at. `count === null` is a rung
 * that was never formed, which is not a rung with zero loads.
 */
function CountBar({
  count,
  scale,
  minSample,
}: {
  count: number | null;
  scale: number;
  minSample: number;
}) {
  const fill = count === null ? 0 : Math.min(100, (count / scale) * 100);
  const threshold = (minSample / scale) * 100;
  const inside = fill >= 24;

  return (
    <div className="countbar">
      {count !== null && <div className="f" style={{ width: `${fill}%` }} />}
      <div className="thr" style={{ left: `${threshold}%` }} />
      <div
        className={inside ? "n" : "n out"}
        style={inside ? undefined : { left: `calc(${Math.max(fill, threshold)}% + 10px)` }}
      >
        {count === null ? "— never formed" : loads(count)}
      </div>
    </div>
  );
}

function NotQueried({ tier, explain }: { tier: LaneTier; explain: boolean }) {
  return (
    <div className="rung off">
      <div className="rung-hd">
        <span className="rung-name mono">{tier}</span>
        <span className="rung-def">{TIER_DEF[tier]}</span>
        <span className="spacer" />
        <span className="chip mute">Not queried</span>
      </div>
      <div className="scope">
        <i style={{ width: `${SCOPE_WIDTH[tier]}%` }} />
      </div>
      <div className="countbar">
        <div className="n out">— not asked</div>
      </div>
      <div className="rung-note">
        {explain
          ? "Never asked — a narrower rung already cleared the minimum, so widening would only add noise."
          : "Never asked."}
      </div>
    </div>
  );
}

/**
 * The same walk in one line, for a panel that has already shown the funnel
 * elsewhere on the screen.
 *
 * The price answer and the ranking walk the same load through the same lookup,
 * so the two should agree rung for rung. Printing both — the funnel for the
 * price, this strip for the ranking — is how a reviewer notices if they ever
 * don't.
 */
export function TierWalkLine({ walk }: { walk: Walk }) {
  return (
    <div className="note">
      <b>Tier walk</b>{" "}
      {walk.rungs.map((rung, index) => (
        <span key={rung.tier}>
          {index > 0 && " → "}
          <span className="mono">{rung.tier}</span>,{" "}
          {rung.skipped ? "never formed" : loads(rung.load_count)},{" "}
          <span className={`chip verdict ${rung.accepted ? "good" : "mute"}`}>
            {rung.verdict}
          </span>
        </span>
      ))}
      {". "}
      Minimum sample {walk.min_sample} loads. Rungs past the accepted one were never asked. The
      funnel above the carriers walks the same load through the same lookup, so the two agree
      rung for rung.
    </div>
  );
}
