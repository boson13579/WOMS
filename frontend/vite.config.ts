/**
 * Vite build / dev-server configuration.
 *
 * Notes:
 *   - The `@/` path alias mirrors the one in tsconfig.json so Bulletproof
 *     React imports work both at type-check time and at bundle time.
 *   - The `/api` proxy forwards browser requests in dev to the FastAPI
 *     container so we avoid CORS/cookie hassles during local development.
 */
import path from 'node:path';

import react from '@vitejs/plugin-react';
/// <reference types="vitest" />
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
