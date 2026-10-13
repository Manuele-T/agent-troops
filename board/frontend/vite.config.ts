import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server only (npm run dev): proxy backend routes so the browser calls same-origin (no
// CORS). Override the target with VITE_BACKEND if the backend runs elsewhere.
const BACKEND = process.env.VITE_BACKEND ?? "http://127.0.0.1:8123";

export default defineConfig({
  plugins: [react()],
  // The page is served by the Python package: teambuild/board/app.py serves teambuild/board/static.
  build: { outDir: "../../teambuild/board/static", emptyOutDir: true },
  server: {
    proxy: {
      "/agents": BACKEND,
      // SSE: disable buffering so events stream through immediately.
      "/events": { target: BACKEND, changeOrigin: true },
    },
  },
});
