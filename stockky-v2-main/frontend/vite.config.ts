/// <reference types="node" />

import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Dev proxy: when VITE_API_URL is empty, relative /api and gateway paths
 * forward to the local/api-gateway (or VITE_BACKEND_URL).
 * Production builds still use absolute VITE_API_URL via getApiUrl().
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target =
    env.VITE_BACKEND_URL ||
    env.VITE_API_URL ||
    "http://localhost:8000";

  const proxyPaths = [
    "/api",
    "/scan",
    "/data-feed",
    "/surprise",
    "/ops",
    "/watchlist",
    "/stock",
    "/notifications",
    "/training",
    "/health",
    "/system",
    "/wake-all",
    "/stockky-hot",
  ];

  const proxy: Record<string, object> = {};
  for (const p of proxyPaths) {
    proxy[p] = {
      target,
      changeOrigin: true,
      secure: false,
    };
  }
    return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      port: Number(env.VITE_DEV_PORT || 5173),
      allowedHosts: true,
      proxy,
      // HMR is only used by the dev server. If you ever run `npm run dev`
      // behind the HTTPS proxy, set VITE_HMR_HOST=stockky.duckdns.org so the
      // HMR client connects over wss://host:443 instead of ws://host:5173
      // (which the proxy drops, causing reload loops). Left undefined by
      // default so plain local dev is unaffected. Production uses `preview`
      // (below), which has no HMR at all.
      hmr: env.VITE_HMR_HOST
        ? { host: env.VITE_HMR_HOST, clientPort: 443, protocol: "wss" }
        : undefined,
    },
    // Production serving: `vite preview` serves the built dist/ as static files
    // with NO HMR websocket — this is what stops the ~5s reload loop. Behind
    // nginx the API calls use absolute VITE_API_URL, but we keep the proxy here
    // too so `vite preview` also works standalone (without nginx) in a pinch.
    preview: {
      host: "0.0.0.0",
      port: Number(env.VITE_PREVIEW_PORT || env.VITE_DEV_PORT || 5173),
      allowedHosts: true,
      proxy,
    },
  };
});
