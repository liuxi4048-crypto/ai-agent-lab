import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const BACKEND = "http://127.0.0.1:8765";
// SSE(/events)含め全APIを FastAPI(8765) へプロキシ
const proxy = Object.fromEntries(
  ["/run", "/runs", "/events", "/models", "/health", "/gpu", "/workspace", "/projects", "/bench"].map(
    (p) => [p, { target: BACKEND, changeOrigin: true }],
  ),
);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, proxy },
  // ビルド成果物は FastAPI が配信する ../static-react へ
  build: { outDir: "../static-react", emptyOutDir: true },
});
