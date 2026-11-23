import { readFileSync } from "node:fs";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { loadEnv, type ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";

// The browser talks to its own origin (http://127.0.0.1:5173) and Vite forwards API
// calls to the NGINX gateway. Same-origin requests need no CORS configuration on the
// backend, and the gateway's rate limiting, request ids and load balancing still apply.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  // 127.0.0.1 rather than localhost: on Windows "localhost" may resolve to ::1 first,
  // which Docker Desktop's IPv4-only port binding does not answer.
  const target = env.LEDGERFLOW_API_PROXY_TARGET || "http://127.0.0.1:8080";
  // The local experiment controller (python -m labctl, 127.0.0.1:8090). Its per-start token
  // is read from ../.ledgerflow/controller-token on every request and added HERE, in the dev
  // server, so the browser never sees it and no other web page can use the controller.
  const controllerTarget = env.LEDGERFLOW_CONTROLLER_PROXY_TARGET || "http://127.0.0.1:8090";
  const tokenFile = path.resolve(import.meta.dirname, "../.ledgerflow/controller-token");
  const proxy: Record<string, ProxyOptions> = {
    "/api": { target, changeOrigin: false },
    "/health": { target, changeOrigin: false },
    "/controller": {
      target: controllerTarget,
      changeOrigin: false,
      configure: (server) => {
        server.on("proxyReq", (proxyReq) => {
          proxyReq.removeHeader("x-ledgerflow-controller-token");
          try {
            proxyReq.setHeader("x-ledgerflow-controller-token", readFileSync(tokenFile, "utf8").trim());
          } catch {
            // No controller has run yet: it will answer 401, or not at all.
          }
        });
      },
    },
  };

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { "@": path.resolve(import.meta.dirname, "./src") },
      // One copy of React no matter how a path is spelled (see scripts/vitest.mjs).
      dedupe: ["react", "react-dom", "@tanstack/react-query", "react-router"],
    },
    server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
    build: { sourcemap: true },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
      restoreMocks: true,
      testTimeout: 15_000,
    },
  };
});
