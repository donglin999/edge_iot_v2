/**
 * /import — Phase 2 P5.2 scope §3.d (the issue calls it /configuration,
 * but the UI route is /import; both refer to the same Excel → write-to
 * config-library flow).
 *
 * Verifies:
 *   1. Upload a known-good Excel via the AntD Upload.Dragger.
 *   2. Validation passes (no row errors).
 *   3. "写入配置库" applies the import and the success Result is shown.
 *   4. A new ``ConfigVersion`` row appears at the top of
 *      ``GET /api/configuration/versions/?ordering=-created_at`` —
 *      proving the import wrote to the config library.
 *
 * MQTT note: the full apply_config round-trip (center → edge over
 * ``edge/<edge>/cmd/apply_config``) is driven by Celery starting an
 * AcqTask. That has a much wider state-prep surface and is exercised
 * by the backend MQTT downlink tests + the chaos scripts. This spec
 * confirms the UI side — the next user step (start task) is observable
 * in ``/fleet`` once an acquisition task is dispatched.
 */
import { expect, test } from '@playwright/test';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { apiContext } from '../helpers/api';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SAMPLE_XLSX = path.join(HERE, '..', 'fixtures', 'xiu108-sample.xlsx');

interface ConfigVersionRow {
  id: number;
  version: number;
  created_at: string;
}

async function latestVersion(): Promise<ConfigVersionRow | null> {
  const ctx = await apiContext();
  try {
    const res = await ctx.get('/api/config/versions/?limit=1&ordering=-created_at');
    if (!res.ok()) return null;
    const body = await res.json();
    const rows: ConfigVersionRow[] = Array.isArray(body) ? body : body?.results ?? [];
    return rows[0] ?? null;
  } finally {
    await ctx.dispose();
  }
}

test.describe('/import — Excel import lands in the config library', () => {
  test('upload → validate → apply → new ConfigVersion appears', async ({ page }, info) => {
    test.setTimeout(120_000);

    const before = await latestVersion();

    await page.goto('/import');
    await expect(page.getByRole('heading', { level: 1, name: /导入作业|Import/ })).toBeVisible();

    // Upload.Dragger renders a hidden <input type="file">; the spec
    // attaches the file directly to it (faster + deterministic than
    // dragging in headless mode).
    const fileInput = page.locator('input[type="file"]').first();
    await fileInput.setInputFiles(SAMPLE_XLSX);

    // After upload, the page goes from step 0 → step 1 ("校验") and
    // renders a 校验结果 card. If validation never runs (no Celery
    // worker — common in the XIU-104 center-only bring-up), the job
    // stays at status=pending and the success Alert never appears.
    // Detect that and skip the spec with an actionable message instead
    // of failing.
    await expect(page.getByText('校验结果')).toBeVisible({ timeout: 30_000 });
    const validateAlert = page.getByText('校验通过', { exact: true });
    const pendingTag = page.locator('.ant-tag', { hasText: /^pending$/ }).first();
    try {
      await expect(validateAlert).toBeVisible({ timeout: 30_000 });
    } catch (err) {
      if (await pendingTag.isVisible().catch(() => false)) {
        test.skip(true, 'import job stuck at status=pending — Celery worker not running on the center stack');
      }
      throw err;
    }
    await page.screenshot({
      path: 'e2e/.artifacts/screenshots/configuration-01-validated.png',
      fullPage: true,
    });

    // Apply with the default 合并 (merge) mode. The success "导入成功"
    // Result is only rendered when the server returns ``apply_result``,
    // which itself requires the Celery worker to have ingested the
    // Excel rows. If the worker is missing the click is a no-op — fall
    // back to the same skip path.
    const applyBtn = page.getByRole('button', { name: /写入配置库/ });
    await expect(applyBtn).toBeEnabled();
    await applyBtn.click();
    try {
      await expect(page.getByText('导入成功')).toBeVisible({ timeout: 30_000 });
    } catch (err) {
      if (await pendingTag.isVisible().catch(() => false)) {
        test.skip(true, 'import job stuck at status=pending after apply — Celery worker not running on the center stack');
      }
      throw err;
    }
    await page.screenshot({
      path: 'e2e/.artifacts/screenshots/configuration-02-applied.png',
      fullPage: true,
    });

    // Confirm a new ConfigVersion was written.
    const after = await latestVersion();
    expect(after, 'no ConfigVersion rows visible at all').not.toBeNull();
    if (before) {
      expect(after!.version, `version did not advance (before=${before.version}, after=${after!.version})`).toBeGreaterThan(
        before.version,
      );
    }
    await info.attach('config-version delta', {
      body: `before=${JSON.stringify(before)}\nafter=${JSON.stringify(after)}`,
      contentType: 'text/plain',
    });
  });
});
