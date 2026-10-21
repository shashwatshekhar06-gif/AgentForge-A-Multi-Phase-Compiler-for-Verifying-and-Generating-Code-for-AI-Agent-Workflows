import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During development, /api requests are forwarded to the FastAPI server on port 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // AF_API_PORT lets a second copy run against another API server (default 8000)
    proxy: { "/api": `http://localhost:${process.env.AF_API_PORT ?? 8000}` },
  },
});
