import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * A fully static page: no API calls, no proxy, nothing to configure. It can be
 * served by any static host (Vercel, Netlify, GitHub Pages, nginx) and the only
 * outbound links are to GitHub.
 */
const PORT = Number(process.env.PORT ?? 5173);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: PORT,
    strictPort: false,
    allowedHosts: true,
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
