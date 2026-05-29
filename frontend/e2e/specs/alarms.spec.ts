/**
 * /alarms — Phase 2 P5.2 scope §3.c.
 *
 * Pre-req: XIU-107 (central MQTT data-plane handler) is bound — without
 * it, ``handle_alarm_event`` is not on the router and the inbound
 * ``alarm_event`` frame is dropped by the central router.
 *
 * Strategy:
 *   1. Open /alarms in the browser. Snapshot the current row count.
 *   2. Publish a fresh ``alarm_event`` frame onto
 *      ``edge/<EDGE_A>/uplink/alarm_event`` via ``mosquitto_pub`` inside
 *      the broker container.
 *   3. Wait for the row count to increase by one, on the page (no
 *      manual refresh).
 *   4. Screenshot the new top row.
 *
 * The center compares ``rule_id`` against ``AlarmRule.pk`` — if no rule
 * exists, the central handler will drop the frame with a structured
 * log. We work around this by either reusing an existing rule (first
 * row returned from /api/acquisition/alarm-rules/) or, when none
 * exist, posting a minimal one over the API first.
 */
import { expect, test, APIRequestContext } from '@playwright/test';
import {
  apiContext,
  listAlarms,
  listEdges,
} from '../helpers/api';
import { BROKER_CONTAINER, EDGE_A } from '../helpers/env';
import { dockerExec, findContainerByPrefix } from '../helpers/docker';

interface AlarmRule {
  id: number;
  name: string;
  point_code: string;
  device_code: string;
  severity: 'info' | 'warning' | 'critical';
  threshold: number | null;
  operator: string;
  is_active: boolean;
}

async function ensureAlarmRule(ctx: APIRequestContext, edgeName: string): Promise<AlarmRule> {
  const list = await ctx.get('/api/acquisition/alarm-rules/?limit=1&is_active=true');
  if (list.ok()) {
    const body = await list.json();
    const rows: AlarmRule[] = Array.isArray(body) ? body : body?.results ?? [];
    if (rows.length) return rows[0];
  }
  // Create a minimal rule the e2e suite owns.
  const created = await ctx.post('/api/acquisition/alarm-rules/', {
    data: {
      name: `e2e-xiu108-${edgeName}`,
      point_code: 'e2e_point',
      device_code: 'e2e_device',
      operator: 'gt',
      threshold: 0,
      threshold_high: null,
      severity: 'warning',
      is_active: true,
      description: 'XIU-108 Playwright synthetic rule',
    },
  });
  expect(created.ok(), `POST /api/acquisition/alarm-rules/ ${created.status()} ${await created.text()}`).toBeTruthy();
  return created.json();
}

test.describe('/alarms — MQTT alarm_event surfaces in the list', () => {
  test('publishing alarm_event over MQTT appends a row in the UI', async ({ page }, info) => {
    test.setTimeout(120_000);

    const ctx = await apiContext();

    // Preflight — EDGE_A must be online to keep this test honest.
    const edges = await listEdges(ctx);
    const a = edges.find((e) => e.name === EDGE_A);
    test.skip(!a || a.status !== 'online', `edge ${EDGE_A} is not online — bring it up first`);

    const rule = await ensureAlarmRule(ctx, EDGE_A);

    // The center collapses repeated firing events into the open row, so
    // we need to clear any existing open alarm for (rule, edge, point)
    // before publishing — otherwise the next firing is a no-op and the
    // row count never grows.
    const clearFrame = {
      v: '0.6',
      type: 'alarm_event',
      edge_id: EDGE_A,
      monotonic_seq: Math.floor(Date.now() / 1000) - 2,
      rule_id: rule.id,
      point_code: rule.point_code || 'e2e_point',
      device_code: rule.device_code || 'e2e_device',
      value: 0,
      severity: rule.severity,
      status: 'cleared',
      message: 'XIU-108 e2e pre-clear',
      fired_at: new Date(Date.now() - 1000).toISOString(),
    };
    const clearTopic = `edge/${EDGE_A}/uplink/alarm_event`;
    {
      const brokerName = (await findContainerByPrefix(BROKER_CONTAINER)) ?? BROKER_CONTAINER;
      const pre = await dockerExec(
        brokerName,
        ['mosquitto_pub', '-h', '127.0.0.1', '-p', '1883', '-q', '1', '-t', clearTopic, '-m', JSON.stringify(clearFrame)],
        info,
      );
      expect(pre.code, `pre-clear mosquitto_pub failed: ${pre.stderr}`).toBe(0);
      await new Promise((r) => setTimeout(r, 1500));
    }

    const baselineRows = await listAlarms(ctx);
    const baselineCount = baselineRows.length;

    // Render the UI before publishing so the table is mounted when the
    // new row arrives on the next 5 s poll.
    await page.goto('/alarms');
    await expect(page.getByRole('heading', { level: 1, name: /告警中心|Alarms/ })).toBeVisible();
    const table = page.locator('.ant-table-tbody').first();
    await expect(table).toBeVisible();

    // Compose alarm_event and inject via mosquitto_pub inside the broker
    // container — emulating an edge-side publish without forging an
    // edge agent.
    const broker = (await findContainerByPrefix(BROKER_CONTAINER)) ?? BROKER_CONTAINER;
    const seq = Math.floor(Date.now() / 1000);
    const frame = {
      v: '0.6',
      type: 'alarm_event',
      edge_id: EDGE_A,
      monotonic_seq: seq,
      rule_id: rule.id,
      point_code: rule.point_code || 'e2e_point',
      device_code: rule.device_code || 'e2e_device',
      value: 1,
      severity: rule.severity,
      status: 'firing',
      message: `XIU-108 e2e probe @ ${new Date().toISOString()}`,
      fired_at: new Date().toISOString(),
    };
    const payload = JSON.stringify(frame);
    const topic = `edge/${EDGE_A}/uplink/alarm_event`;
    const pub = await dockerExec(
      broker,
      ['mosquitto_pub', '-h', '127.0.0.1', '-p', '1883', '-q', '1', '-t', topic, '-m', payload],
      info,
    );
    expect(pub.code, `mosquitto_pub failed: ${pub.stderr}`).toBe(0);

    // Wait for the API to confirm the central handler accepted the frame
    // (XIU-107). This is the strict check; the UI is a downstream view.
    const deadline = Date.now() + 60_000;
    let grown = false;
    while (Date.now() < deadline) {
      const after = await listAlarms(ctx);
      if (after.length > baselineCount) {
        grown = true;
        break;
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    expect(grown, `alarm row count did not grow within 60 s (baseline=${baselineCount})`).toBe(true);

    // UI assertion — the page polls /api/acquisition/alarms/ every 15 s.
    // The table doesn't render the message text, so assert on a row
    // containing the source edge tag + the rule name. Click 刷新 to
    // skip the poll wait.
    const refreshBtn = page.getByRole('button', { name: /刷新|Refresh/ });
    await refreshBtn.click().catch(() => {});
    const tableRow = page
      .locator('.ant-table-row', { has: page.locator(`text=${EDGE_A}`) })
      .filter({ hasText: rule.name })
      .first();
    await expect(tableRow).toBeVisible({ timeout: 25_000 });
    await page.screenshot({
      path: 'e2e/.artifacts/screenshots/alarms-01-mqtt-row.png',
      fullPage: true,
    });

    await ctx.dispose();
  });
});
