/**
 * Two screens, so navigation is one piece of state: which load id is selected.
 *
 * No router. Two screens do not need URL routing, a route table, or a history
 * integration, and adding React Router here would be the same mistake as
 * turning three TMS adapters into a plugin framework. `null` is the list;
 * anything else is that load's detail.
 *
 * The chrome is a sticky topbar: tenant, status filter, and the theme control.
 * Terminals get used in bright rooms and dark ones, so both themes are real and
 * "System" is the default rather than an afterthought.
 */

import { useEffect, useState } from "react";

import { listBrokers } from "./api";
import { LoadDetail } from "./LoadDetail";
import { LoadList } from "./LoadList";
import type { Broker, LoadStatus } from "./types";
import { LOAD_STATUSES } from "./types";
import { useApi } from "./useApi";

/** The status filter's "no filter" option — sent as no `status` parameter. */
const ALL = "ALL";

type Theme = "light" | "dark" | "system";

const THEMES: readonly Theme[] = ["light", "dark", "system"];
const THEME_KEY = "carrier-pool.theme";

function readStoredTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
  } catch {
    // Private browsing, or storage disabled. The default is fine.
  }
  return "system";
}

export function App() {
  const brokers = useApi<Broker[]>("brokers", listBrokers);
  const [brokerId, setBrokerId] = useState<string | null>(null);
  const [status, setStatus] = useState<LoadStatus | typeof ALL>("ACTIVE");
  const [selectedLoadId, setSelectedLoadId] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(readStoredTheme);

  // `system` means "no override": the stylesheet's `prefers-color-scheme`
  // block does the work, so the attribute is removed rather than set.
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Not being able to remember the choice is not worth failing a render.
    }
  }, [theme]);

  // Pick the first broker once the list arrives; the user can change it. Done
  // in an effect rather than at fetch time so the dropdown stays a controlled
  // input with exactly one source of truth.
  const firstBrokerId = brokers.state === "ready" ? (brokers.data[0]?.id ?? null) : null;
  useEffect(() => {
    setBrokerId((current) => current ?? firstBrokerId);
  }, [firstBrokerId]);

  return (
    <>
      <div className="topbar">
        <div className="brand">
          Carrier Pool <span>/ coverage</span>
        </div>

        <label className="field">
          Broker{" "}
          {brokers.state === "error" ? (
            <span className="error">unavailable: {brokers.message}</span>
          ) : (
            <select
              value={brokerId ?? ""}
              disabled={brokers.state !== "ready"}
              onChange={(event) => {
                setBrokerId(event.target.value);
                // A load id is only unique within a broker, so carrying the
                // selection across a broker change would ask one tenant for
                // another tenant's load.
                setSelectedLoadId(null);
              }}
            >
              {brokers.state !== "ready" && <option value="">loading…</option>}
              {brokers.state === "ready" &&
                brokers.data.map((broker) => (
                  <option key={broker.id} value={broker.id}>
                    {broker.name} (TMS {broker.tms_type})
                  </option>
                ))}
            </select>
          )}
        </label>

        <label className="field">
          Status{" "}
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value as LoadStatus | typeof ALL)}
          >
            <option value={ALL}>All statuses</option>
            {LOAD_STATUSES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>

        <div className="spacer" />

        <div className="seg" role="group" aria-label="Colour theme">
          {THEMES.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={theme === value}
              onClick={() => setTheme(value)}
            >
              {value === "system" ? "System" : value === "light" ? "Light" : "Dark"}
            </button>
          ))}
        </div>
      </div>

      <main className="wrap">
        {brokerId === null ? (
          <p className="state">
            {brokers.state === "error"
              ? "No broker selected — the broker list could not be fetched."
              : "Loading brokers…"}
          </p>
        ) : selectedLoadId === null ? (
          <LoadList
            brokerId={brokerId}
            status={status === ALL ? null : status}
            onSelect={setSelectedLoadId}
          />
        ) : (
          <LoadDetail
            brokerId={brokerId}
            loadId={selectedLoadId}
            onBack={() => setSelectedLoadId(null)}
          />
        )}
      </main>
    </>
  );
}
