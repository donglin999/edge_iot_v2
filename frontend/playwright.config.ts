/**
 * Playwright config — XIU-108 Phase 2 P5.2.
 *
 * Drives the four MQTT-form acceptance specs in ``e2e/`` against the
 * Vite dev server (default http://localhost:5173). The dev server is
 * NOT auto-started here — XIU-108 assumes the operator has already
 * brought up docker-compose.center.yml + 2 docker-compose.edge.yml
 * stacks (see ``scripts/qa_xiu108_playwright.sh``).
 *
 * Overrides:
 *   E2E_BASE_URL          target UI origin (default http://localhost:5173)
 *   E2E_API_URL           target backend origin used by the helpers when
 *                         they need a direct hit instead of going through
 *                         the dev-server proxy (default http://localhost:8000)
 *   E2E_EDGE_A / EDGE_B   edge IDs that must come online for fleet/alarms
 *                         specs (default qa-xiu108-a / qa-xiu108-b)
 */
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:5173';

export default defineConfig({
  testDir: './e2e/specs',
  outputDir: './e2e/.artifacts/test-results',
  snapshotDir: './e2e/snapshots',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: './e2e/.artifacts/html-report', open: 'never' }],
    ['json', { outputFile: './e2e/.artifacts/results.json' }],
  ],
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
