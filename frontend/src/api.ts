/**
 * The only place in the frontend that talks to the network.
 *
 * Everything goes through `getJson`, so every failure mode — unreachable dev
 * proxy, 404, an unknown broker, a body that isn't JSON — arrives at the UI as
 * one `ApiError` with a message a person can read. A panel that renders nothing
 * is indistinguishable from a bug.
 *
 * ## broker_id on load routes
 *
 * PRD section 10 writes the load routes as `/api/loads/{id}`, with no broker in
 * the path. But `source_load_id` is only unique *within* a broker
 * (`UNIQUE (broker_id, source_load_id)`), and the repository refuses to run any
 * tenant query without a broker binding — `current_broker()` raises when
 * `app.broker_id` is unset. So the broker has to reach the server somehow.
 *
 * `backend/app/api/deps.py` binds it as a required `broker_id` **query
 * parameter** on every route except `/api/brokers` and `/api/health`, which is
 * what the four URL builders below send.
 */

import type {
  Broker,
  Health,
  Load,
  LoadDetail,
  LoadStatus,
  PriceEstimate,
  Recommendations,
} from "./types";

/**
 * A failed call, with enough on it to say *why* in the UI.
 *
 * `status === 0` means the request never reached a server (dev proxy down,
 * network refused) — a different problem from a server that answered badly, and
 * worth telling the user apart.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** True for the abort we cause ourselves when a fetch is superseded. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** FastAPI's error body is `{"detail": ...}`; anything else we show verbatim. */
async function describeFailure(response: Response): Promise<string> {
  const body = await response.text().catch(() => "");
  if (!body) return `HTTP ${response.status} ${response.statusText}`.trim();
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      return `HTTP ${response.status}: ${
        typeof detail === "string" ? detail : JSON.stringify(detail)
      }`;
    }
  } catch {
    // Not JSON — a Vite proxy error, an HTML page. The raw text says more than
    // "HTTP 500" does, so keep a readable slice of it.
  }
  return `HTTP ${response.status}: ${body.slice(0, 300)}`;
}

async function getJson<T>(path: string, signal: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" }, signal });
  } catch (error) {
    if (isAbort(error)) throw error;
    throw new ApiError(0, `cannot reach the API at ${path} (${String(error)})`);
  }

  if (!response.ok) {
    throw new ApiError(response.status, await describeFailure(response));
  }

  try {
    return (await response.json()) as T;
  } catch (error) {
    throw new ApiError(response.status, `${path} did not return JSON (${String(error)})`);
  }
}

function loadQuery(brokerId: string, extra?: Record<string, string>): string {
  const params = new URLSearchParams({ broker_id: brokerId, ...extra });
  return params.toString();
}

export function getHealth(signal: AbortSignal): Promise<Health> {
  return getJson<Health>("/api/health", signal);
}

export function listBrokers(signal: AbortSignal): Promise<Broker[]> {
  return getJson<Broker[]>("/api/brokers", signal);
}

/** `status === null` means no filter — all statuses for the broker. */
export function listLoads(
  brokerId: string,
  status: LoadStatus | null,
  signal: AbortSignal,
): Promise<Load[]> {
  const query = loadQuery(brokerId, status ? { status } : undefined);
  return getJson<Load[]>(`/api/loads?${query}`, signal);
}

/**
 * The detail route returns more than the list route: `LoadDetail` adds the
 * carrier, the customer and the full sync history (U6) to the same `Load`.
 */
export function getLoad(brokerId: string, loadId: string, signal: AbortSignal): Promise<LoadDetail> {
  const path = `/api/loads/${encodeURIComponent(loadId)}?${loadQuery(brokerId)}`;
  return getJson<LoadDetail>(path, signal);
}

export function getPriceEstimate(
  brokerId: string,
  loadId: string,
  signal: AbortSignal,
): Promise<PriceEstimate> {
  const path = `/api/loads/${encodeURIComponent(loadId)}/price-estimate?${loadQuery(brokerId)}`;
  return getJson<PriceEstimate>(path, signal);
}

export function getRecommendations(
  brokerId: string,
  loadId: string,
  signal: AbortSignal,
): Promise<Recommendations> {
  const path = `/api/loads/${encodeURIComponent(loadId)}/recommendations?${loadQuery(brokerId)}`;
  return getJson<Recommendations>(path, signal);
}
