/**
 * One hook, so every fetch in the app has the same three visible outcomes.
 *
 * `Async<T>` is a discriminated union rather than the usual
 * `{ data, loading, error }` triple, because the triple lets you render the
 * `data === null && !loading && !error` corner — a blank panel that looks
 * exactly like a bug. Here that state does not typecheck: a caller has to say
 * what it draws for each of idle / loading / error / ready.
 */

import { useEffect, useRef, useState } from "react";

import { ApiError, isAbort } from "./api";

export type Async<T> =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

const IDLE: Async<never> = { state: "idle" };
const LOADING: Async<never> = { state: "loading" };

/**
 * Run `fetcher` whenever `key` changes, tracking loading and error state.
 *
 * `key` is the whole dependency: it must contain every input the fetcher reads
 * (broker, load id, status filter), and `key === null` means "nothing to fetch
 * yet" — the idle state, which is how the load list waits for the broker list
 * to arrive before asking for loads.
 *
 * The fetcher is held in a ref, so passing a fresh closure on every render
 * (which is the natural way to write the call site) does not re-fire the
 * request. In-flight requests are aborted when the key changes, and an aborted
 * request never writes state — so React 19's StrictMode double-invoke shows a
 * loading state, not a spurious error.
 */
export function useApi<T>(
  key: string | null,
  fetcher: (signal: AbortSignal) => Promise<T>,
): Async<T> {
  const [result, setResult] = useState<Async<T>>(key === null ? IDLE : LOADING);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (key === null) {
      setResult(IDLE);
      return;
    }

    const controller = new AbortController();
    setResult(LOADING);

    fetcherRef
      .current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setResult({ state: "ready", data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || isAbort(error)) return;
        setResult({
          state: "error",
          message: error instanceof ApiError ? error.message : String(error),
        });
      });

    return () => controller.abort();
  }, [key]);

  return result;
}
