import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * The page talks to the agent with same-origin URLs (`/health`, `/ollama/...`).
 * In development Vite proxies those to the FastAPI server, so the browser never
 * needs to know about ports, CORS or `localhost` (which would break in a
 * remote preview sandbox).
 */
const API_TARGET = process.env.NOUR_CODE_API ?? "http://127.0.0.1:8000";
const PORT = Number(process.env.PORT ?? 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: PORT,
    strictPort: false,
    allowedHosts: true,
    proxy: {
      "/health": { target: API_TARGET, changeOrigin: true },
      "/ollama": { target: API_TARGET, changeOrigin: true },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 4173,
    allowedHosts: true,
  },
  build: {
    target: "es2022",
    sourcemap: false,
  },
});
