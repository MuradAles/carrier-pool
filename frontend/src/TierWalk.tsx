/**
 * The tier walk, rendered the same way wherever it appears.
 *
 * Invariant 6 says every lane answer reports which tier it used and how many
 * loads backed it. Reporting only the *winning* rung satisfies the letter of
 * that and not the point: "ZIP3, 12 loads" and "METRO, 31 loads because ZIP3
 * had 2" are different claims, and only the second lets a broker judge the
 * answer. So every rung the walk tried is listed, with its count and the
 * verdict the domain wrote for it.
 *
 * `verdict` is printed, never rebuilt. The backend already has the sentence
 * ("ACCEPTED", "rejected, 2 < 5", "skipped, lane end not on the map"), and a
 * second construction of it from `load_count` and `accepted` here is a second
 * thing to get wrong.
 *
 * Both the price estimate and the ranking walk the same load through the same
 * lookup, so the two tables should agree rung for rung. Showing both is how a
 * reviewer notices if they ever don't.
 */

import { equipmentFilter, loads, UNKNOWN } from "./format";
import type { TierWalk as Walk } from "./types";

export function TierWalkTable({ walk }: { walk: Walk }) {
  return (
    <div className="walk">
      <h4>Tier walk</h4>
      <p className="note">
        Narrowest rung first, stopping at the first one with at least {walk.min_sample} loads.
        Rungs below the accepted one are absent because they were never asked — not because
        they were empty.
      </p>
      <table>
        <thead>
          <tr>
            <th>Rung</th>
            <th>Lane key</th>
            <th>Equipment</th>
            <th className="num">Loads</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {walk.rungs.map((rung) => (
            <tr key={rung.tier} className={rung.accepted ? "accepted-rung" : undefined}>
              <td>{rung.tier}</td>
              {/* A skipped rung has no key at all: the load's lane end is not on
                  the map, so the question could not be formed. That is not the
                  same as asking and finding nothing. */}
              <td>{rung.lane_key ?? UNKNOWN}</td>
              <td>{equipmentFilter(rung.equipment)}</td>
              <td className="num">{rung.skipped ? UNKNOWN : rung.load_count}</td>
              <td>{rung.verdict}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {walk.tier === null && (
        <p className="warn">
          No rung cleared the {walk.min_sample}-load minimum, so this answer has no lane
          evidence behind it.
        </p>
      )}
      {/* Two different routes to an unfiltered pool, and a reader needs to know
          which one this was: the load's own equipment was never known, or the
          filtered rungs were all too thin and the walk gave the filter up. */}
      {walk.tier !== null && !walk.equipment_filtered && (
        <p className="warn">
          {walk.equipment === "ANY"
            ? "This load's equipment is UNKNOWN, so no rung applied an equipment filter."
            : `No ${equipmentFilter(walk.equipment).toLowerCase()} rung cleared the minimum, so the accepted rung dropped the equipment filter.`}{" "}
          These rates mix trailer types and are not like-for-like.
        </p>
      )}
      {walk.tier !== null && (
        <p className="note">
          Accepted: {walk.tier}, {loads(walk.load_count)}, {equipmentFilter(walk.equipment_filter)}.
        </p>
      )}
    </div>
  );
}
