/**
 * /fleet — Phase 2 P5.2 scope §3.a.
 *
 * Verifies:
 *   1. With 2 edges connected over MQTT, the fleet table shows them
 *      both as "在线" (online).
 *   2. ``docker stop center-mosquitto`` for 30 s flips both edges to
 *      "离线" (offline) via the LWT path (XIU-103 / P4).
 *   3. ``docker start center-mosquitto`` returns both to "在线" within
 *      30 s (LWT online retain re-published on reconnect).
 *
 * The spec is conservative — if the 2 expected edges aren't already
 * registered when it starts (e.g. only the center stack is up), it
 * test.skip()s with a clear message instead of failing. The
 * ``scripts/qa_xiu108_playwright.sh`` orchestrator is what brings the
 * full stack up before invoking Playwright.
 */
import { expect, test } from '@playwright/test';
import {
  apiContext,
  listEdges,
  waitForEdgeStatus,
} from '../helpers/api';
import {
  BROKER_CONTAINER,
  EDGE_A,
  EDGE_B,
} from '../helpers/env';
import {
  findContainerByPrefix,
  startContainer,
  stopContainer,
} from '../helpers/docker';

test.describe('/fleet — dual-edge MQTT online/offline', () => {
  test('shows 2 online, broker stop flips to offline, restart returns online', async ({
    page,
  }, info) => {
    test.setTimeout(180_000);

    // --- preflight via API to confirm the stack is ready ------------------
    const ctx = await apiContext();
    const initial = await listEdges(ctx);
    const a = initial.find((e) => e.name === EDGE_A);
    const b = initial.find((e) => e.name === EDGE_B);
    test.skip(
      !a || !b,
      `expected edges ${EDGE_A} and ${EDGE_B} not registered — bring up docker-compose.edge.yml × 2 first`,
    );

    // Wait until both edges are online via the API before driving the UI.
    await waitForEdgeStatus(ctx, EDGE_A, 'online', 60_000);
    await waitForEdgeStatus(ctx, EDGE_B, 'online', 60_000);

    // --- step 1: UI shows both as 在线 --------------------------------------
    await page.goto('/fleet');
    await expect(page.getByRole('heading', { level: 1, name: /边缘节点|Fleet/i })).toBeVisible();
    const rowA = page.getByRole('row', { name: new RegExp(EDGE_A) });
    const rowB = page.getByRole('row', { name: new RegExp(EDGE_B) });
    await expect(rowA).toBeVisible();
    await expect(rowB).toBeVisible();
    await expect(rowA.getByText('在线', { exact: true })).toBeVisible();
    await expect(rowB.getByText('在线', { exact: true })).toBeVisible();
    await page.screenshot({
      path: 'e2e/.artifacts/screenshots/fleet-01-both-online.png',
      fullPage: true,
    });

    // --- step 2: kill broker, wait for both to flip 离线 -------------------
    const broker = (await findContainerByPrefix(BROKER_CONTAINER)) ?? BROKER_CONTAINER;
    const stopRes = await stopContainer(broker, info);
    expect(stopRes.code, `docker stop ${broker} failed: ${stopRes.stderr}`).toBe(0);

    // Center LWT handler should flip both edges to offline within ~30 s
    // (mosquitto WILL publishes when keepalive lapses).
    await waitForEdgeStatus(ctx, EDGE_A, 'offline', 90_000);
    await waitForEdgeStatus(ctx, EDGE_B, 'offline', 90_000);
    await page.getByRole('button', { name: /刷新|Refresh/ }).click().catch(() => {});
    await expect(rowA.getByText('离线', { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(rowB.getByText('离线', { exact: true })).toBeVisible({ timeout: 15_000 });
    await page.screenshot({
      path: 'e2e/.artifacts/screenshots/fleet-02-both-offline.png',
      fullPage: true,
    });

    // --- step 3: restart broker, wait for both to return 在线 --------------
    const startRes = await startContainer(broker, info);
    expect(startRes.code, `docker start ${broker} failed: ${startRes.stderr}`).toBe(0);
    await waitForEdgeStatus(ctx, EDGE_A, 'online', 90_000);
    await waitForEdgeStatus(ctx, EDGE_B, 'online', 90_000);
    await page.getByRole('button', { name: /刷新|Refresh/ }).click().catch(() => {});
    await expect(rowA.getByText('在线', { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(rowB.getByText('在线', { exact: true })).toBeVisible({ timeout: 15_000 });
    await page.screenshot({
      path: 'e2e/.artifacts/screenshots/fleet-03-recovered.png',
      fullPage: true,
    });

    await ctx.dispose();
  });
});
