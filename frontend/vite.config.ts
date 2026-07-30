import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    // In compose the backend is reachable by service name; outside it, localhost.
    proxy: {
      "/api": {
        target: process.env.API_TARGET ?? "http://backend:8000",
        changeOrigin: true,
      },
    },
  },
});
