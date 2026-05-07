import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';
import path from 'node:path';

// dashboard/web — Vite dev server runs on :5173 and proxies /api/* to the
// Hono dashboard server on :3141 (set by DASHBOARD_PORT). Production
// builds emit to dist/ and Hono serves them same-origin.

export default defineConfig({
  plugins: [preact()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:3141',
        changeOrigin: true,
        // SSE needs raw streaming.
        ws: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2022',
  },
});
