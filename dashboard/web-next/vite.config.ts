import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const dashboardProxyTarget = process.env.DASHBOARD_PROXY_TARGET || 'http://127.0.0.1:3141';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5174,
    proxy: {
      '/api': {
        target: dashboardProxyTarget,
        changeOrigin: true,
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
