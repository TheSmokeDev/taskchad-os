import { defineConfig } from 'vitest/config';
import preact from '@preact/preset-vite';
import path from 'node:path';

export default defineConfig({
  plugins: [preact()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    setupFiles: ['./src/__tests__/setup.ts'],
    css: false,
    // Tests must NOT hit the network. Anti-pattern tests grep the source
    // tree at runtime; donor-pages-lane-aware uses the real api.ts shape
    // with mocked fetch.
  },
});
