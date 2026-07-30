import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

type Health = { status: string; database: string; data_dir: string };

function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", lineHeight: 1.5 }}>
      <h1>Carrier Pool</h1>
      <p>Scaffold is up. The load list and load detail screens go here.</p>
      <h2>Backend</h2>
      {error && <p style={{ color: "crimson" }}>Cannot reach backend: {error}</p>}
      {health && (
        <ul>
          <li>status: {health.status}</li>
          <li>database: {health.database}</li>
          <li>data dir: {health.data_dir}</li>
        </ul>
      )}
      {!health && !error && <p>Checking…</p>}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
