import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// In Docker Compose the backend is reachable as http://backend:8000; when the
// dev server runs natively it is on localhost. The browser always calls the
// same origin (/api), so no CORS configuration is needed in development.
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: apiProxyTarget, changeOrigin: true },
    },
  },
  preview: { port: 4173, strictPort: true },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    restoreMocks: true,
  },
})
