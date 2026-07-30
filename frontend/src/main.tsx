import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { getHealth } from "./api";
import type { Health } from "./types";
import { useApi } from "./useApi";
import "./styles.css";

/**
 * A one-line footer saying whether the backend and its data are reachable.
 *
 * Kept from the scaffold because while the API is half-built it answers the
 * first question anyone debugging this screen will ask: is the panel empty
 * because there is no data, or because nothing is running?
 */
function HealthFooter() {
  const health = useApi<Health>("health", getHealth);

  return (
    <footer>
      {health.state === "loading" && "backend: checking…"}
      {health.state === "idle" && "backend: not checked"}
      {health.state === "error" && <span className="error">backend: {health.message}</span>}
      {health.state === "ready" && (
        <>
          backend: {health.data.status} · database: {health.data.database} · data:{" "}
          {health.data.data_dir}
        </>
      )}
    </footer>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    <HealthFooter />
  </StrictMode>,
);
