/**
 * Fleet (edge node) API client (XIU-51 / M1).
 *
 * Backend endpoints:
 *   GET  /api/fleet/edges/   — list edges (server sweeps stale → offline first)
 *   POST /api/fleet/edges/   — factory-register, returns one-shot activation_token
 *
 * The list endpoint goes through DRF global pagination (XIU-9 / H10), so we
 * walk every page via fetchAllPages and hand back a plain array to callers.
 */
import { apiClient } from './apiClient';
import { fetchAllPages } from './pagination';

export type EdgeStatus = 'pending' | 'online' | 'offline';

export interface EdgeNode {
  id: number;
  name: string;
  status: EdgeStatus;
  version: string;
  labels: Record<string, string>;
  last_seen: string | null;
  created_at: string;
  updated_at: string;
}

export interface RegisterEdgePayload {
  name: string;
  labels?: Record<string, string>;
}

/** EdgeNode plus the one-shot plaintext activation token (only on create). */
export interface RegisteredEdge extends EdgeNode {
  activation_token: string;
}

export async function listEdges(signal?: AbortSignal): Promise<EdgeNode[]> {
  return fetchAllPages<EdgeNode>(async (limit, offset) => {
    const res = await apiClient.get('/fleet/edges/', {
      params: { limit, offset },
      signal,
    });
    return res.data;
  });
}

export async function registerEdge(payload: RegisterEdgePayload): Promise<RegisteredEdge> {
  const res = await apiClient.post<RegisteredEdge>('/fleet/edges/', payload);
  return res.data;
}
