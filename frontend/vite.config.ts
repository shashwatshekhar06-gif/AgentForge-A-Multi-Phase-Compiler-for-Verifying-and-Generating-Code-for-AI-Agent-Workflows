import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During development, /api requests are forwarded to the FastAPI server on port 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
});
