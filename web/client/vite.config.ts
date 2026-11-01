import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies API and chat socket to the web process (default port 8766).
const webTarget = process.env.ASSISTANT_WEB_URL ?? "http://127.0.0.1:8766";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": webTarget,
      "/ws": { target: webTarget.replace(/^http/, "ws"), ws: true },
    },
  },
});
