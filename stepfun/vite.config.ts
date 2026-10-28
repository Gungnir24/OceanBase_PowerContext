import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const crossOriginHeaders = {
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Embedder-Policy": "require-corp",
  "Cross-Origin-Resource-Policy": "same-origin",
};

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiPort = Number(env.PORT || 8787);
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      headers: crossOriginHeaders,
      proxy: {
        "/api": `http://127.0.0.1:${apiPort}`,
      },
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
      headers: crossOriginHeaders,
    },
    build: {
      target: "es2022",
      sourcemap: false,
      chunkSizeWarningLimit: 2000,
    },
  };
});
