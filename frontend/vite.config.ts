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
    },
  };
});