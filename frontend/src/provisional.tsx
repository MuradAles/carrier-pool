/**
 * ===========================================================================
 * THE SEAM. This file is the whole reconciliation job for U4 and U5.
 * ===========================================================================
 *
 * `/api/loads/{id}/price-estimate` and `/api/loads/{id}/recommendations` return
 * 501 today — scoring (PRD section 8) and pricing (section 9) are not built. So
 * these two panels do not render fields; they render the honest status of the
 * call. That is deliberate: binding a UI to field names invented ahead of the
 * payload produces rework, and a panel that silently draws nothing while the
 * endpoint is missing is the exact failure mode this app is trying not to have.
 *
 * When Phase 7 lands the endpoints, everything that changes is here and in the
 * PROVISIONAL block of `types.ts`:
 *
 *   1. Reconcile `PriceEstimate` and `CarrierRecommendation` in `types.ts` with
 *      the real response bodies.
 *   2. Replace the `ready` branch of each panel below with the real rendering.
 *
 * Two rules the finished panels must keep, both from CLAUDE.md:
 *
 *   * **Render, never compute.** Point estimate, range, confidence label and
 *      every reason string arrive finished. The browser does no arithmetic on
 *      them. A percentage the frontend derives is a second source of truth that
 *      will eventually disagree with the score.
 *   * **Show provenance next to the answer.** Tier, load count and confidence
 *      are part of the estimate, not a footnote — invariant 6. A low-confidence
 *      estimate must be visibly labelled, never styled the same as a high one.
 *      And reasons render in the order the API gave them: no re-sorting, no
 *      truncating to the top three, no dropping a zero-score carrier.
 */

import { getPriceEstimate, getRecommendations } from "./api";
import type { PriceEstimate, Recommendations } from "./types";
import { useApi } from "./useApi";

interface Props {
  brokerId: string;
  loadId: string;
}

export function PriceEstimatePanel({ brokerId, loadId }: Props) {
  const estimate = useApi<PriceEstimate>(`${brokerId}|${loadId}`, (signal) =>
    getPriceEstimate(brokerId, loadId, signal),
  );

  return (
    <section>
      <h3>Price estimate</h3>
      {estimate.state === "loading" && <p className="note">Loading estimate…</p>}
      {estimate.state === "idle" && <p className="note">No load selected.</p>}
      {estimate.state === "error" && <p className="error">{estimate.message}</p>}
      {estimate.state === "ready" && (
        <p className="note">
          Endpoint answered, but this panel is not wired to its fields yet (U4).
        </p>
      )}
    </section>
  );
}

export function RankedCarriersPanel({ brokerId, loadId }: Props) {
  const recommendations = useApi<Recommendations>(`${brokerId}|${loadId}`, (signal) =>
    getRecommendations(brokerId, loadId, signal),
  );

  return (
    <section>
      <h3>Ranked carriers</h3>
      {recommendations.state === "loading" && <p className="note">Loading carriers…</p>}
      {recommendations.state === "idle" && <p className="note">No load selected.</p>}
      {recommendations.state === "error" && <p className="error">{recommendations.message}</p>}
      {recommendations.state === "ready" && (
        <p className="note">
          Endpoint answered, but this panel is not wired to its fields yet (U5).
        </p>
      )}
    </section>
  );
}
