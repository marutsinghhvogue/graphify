import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Flask JSON API (graphify serve-web) defaults to 127.0.0.1:8756.
// Proxy /api and /health there so the browser only ever talks to the Vite
// origin — no CORS setup needed on the Python side. Override the backend with
// GRAPHIFY_API=http://host:port when running `npm run dev`.
const backend = process.env.GRAPHIFY_API || "http://127.0.0.1:8756";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/health": { target: backend, changeOrigin: true },
    },
  },
});
