import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';
import path from 'node:path';

// dashboard/web — Vite dev server runs on :5473 and proxies /api/* to the
// Hono dashboard server on :3141 (set by DASHBOARD_PORT). Production
// builds emit to dist/ and Hono serves them same-origin.
// 5473 is deliberate: this machine's Windows excluded-port ranges cover
// the Vite default 5173 (5168–5267) and 5358–5457, so `npm run dev` fails
// with listen EACCES on the default port.

const dashboardProxyTarget = process.env.DASHBOARD_PROXY_TARGET || 'http://127.0.0.1:3141';

export default defineConfig({
  plugins: [preact()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5473,
    proxy: {
      '/api': {
        target: dashboardProxyTarget,
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
