/**
 * Lightweight HTTP helpers for talking to the center API directly from
 * the Playwright specs (bypassing the Vite dev-server proxy when we
 * just need to seed / read state for a fixture).
 */
import { request, APIRequestContext } from '@playwright/test';
import { API_URL } from './env';

export async function apiContext(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: API_URL });
}

export interface EdgeNodeLite {
  id: number;
  name: string;
  status: 'online' | 'offline' | 'pending';
  last_seen: string | null;
}

export async function listEdges(ctx: APIRequestContext): Promise<EdgeNodeLite[]> {
  const res = await ctx.get('/api/fleet/edges/?limit=200');
  if (!res.ok()) {
    throw new Error(`GET /api/fleet/edges/ ${res.status()} ${await res.text()}`);
  }
  const body = await res.json();
  if (Array.isArray(body)) return body as EdgeNodeLite[];
  if (Array.isArray(body?.results)) return body.results as EdgeNodeLite[];
  return [];
}

export interface AlarmRow {
  id: number;
  rule_name: string;
  severity: 'info' | 'warning' | 'critical';
  point_code: string;
  device_code: string;
  status: string;
  fired_at: string;
  edge: number | null;
  edge_name: string | null;
}

export async function listAlarms(ctx: APIRequestContext): Promise<AlarmRow[]> {
  const res = await ctx.get('/api/acquisition/alarms/?limit=50&ordering=-fired_at');
  if (!res.ok()) {
    throw new Error(`GET /api/acquisition/alarms/ ${res.status()} ${await res.text()}`);
  }
  const body = await res.json();
  if (Array.isArray(body)) return body as AlarmRow[];
  if (Array.isArray(body?.results)) return body.results as AlarmRow[];
  return [];
}

export async function waitForEdgeStatus(
  ctx: APIRequestContext,
  edgeName: string,
  want: 'online' | 'offline',
  timeoutMs = 30_000,
): Promise<EdgeNodeLite> {
  const deadline = Date.now() + timeoutMs;
  let last: EdgeNodeLite | null = null;
  while (Date.now() < deadline) {
    const edges = await listEdges(ctx);
    const match = edges.find((e) => e.name === edgeName);
    if (match) {
      last = match;
      if (match.status === want) return match;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(
    `edge ${edgeName} never reached status=${want} within ${timeoutMs}ms (last=${JSON.stringify(last)})`,
  );
}
