import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end tests against a running stack (`make up`, or the dev server plus
 * backend). They use real email (Mailpit), real uploads and processing.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 180_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'off',
    screenshot: 'only-on-failure',
    // Production smoke tests (deploy/production/smoke): a local CA and a hostname
    // mapped to 127.0.0.1.
    ignoreHTTPSErrors: process.env.E2E_IGNORE_HTTPS_ERRORS === '1',
    launchOptions: process.env.E2E_HOST_RESOLVER_RULES
      ? { args: [`--host-resolver-rules=${process.env.E2E_HOST_RESOLVER_RULES}`, '--no-proxy-server'] }
      : {},
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1360, height: 860 } } },
  ],
})
