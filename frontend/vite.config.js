import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser talks to Vite; Vite forwards /api and /media to Flask (no CORS setup needed).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:5000", "/media": "http://127.0.0.1:5000" } },
});
