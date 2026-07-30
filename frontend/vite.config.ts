import { lookup } from "node:dns/promises";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Where `/api/*` is proxied to.
 *
 * Inside compose the backend answers to its service name; a bare `npm run dev`
 * on a laptop reaches it on localhost. Rather than requiring an env var be set
 * correctly in one of the two cases, ask DNS once at config time which world we
 * are in. `API_TARGET` still overrides, for anything neither of those covers.
 */
async function apiTarget(): Promise<string> {
  if (process.env.API_TARGET) return process.env.API_TARGET;
  try {
    await lookup("backend");
    return "http://backend:8000";
  } catch {
    return "http://localhost:8000";
  }
}

export default defineConfig(async () => ({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: await apiTarget(),
        changeOrigin: true,
      },
    },
  },
}));
