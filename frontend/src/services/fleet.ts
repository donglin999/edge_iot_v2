/**
 * Fleet (edge node) API client (XIU-51 / M1, XIU-60 / M2).
 *
 * Backend endpoints:
 *   GET  /api/fleet/edges/         — list edges (server sweeps stale → offline first)
 *   POST /api/fleet/edges/         — factory-register, returns one-shot activation_token
 *   GET  /api/fleet/task-statuses/ — latest per-(edge, task) lifecycle state
 *
 * The list endpoints go through DRF global pagination (XIU-9 / H10), so we
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

/** Lifecycle state an edge reports for one of its dispatched tasks (M2). */
export type EdgeTaskState =
  | 'starting'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'error';

/** Latest reported state for one (edge, task) pairing — one row per pair. */
export interface EdgeTaskStatus {
  id: number;
  edge: number;
  edge_name: string;
  task: number;
  task_code: string;
  state: EdgeTaskState;
  error: string;
  last_reported_at: string;
  updated_at: string;
}

export interface TaskStatusFilters {
  /** Restrict to a single AcqTask id. */
  task?: number;
  /** Restrict to a single EdgeNode id. */
  edge?: number;
}

/**
 * List EdgeTaskStatus rows, optionally filtered by `?task=` / `?edge=`.
 *
 * This is the projection the `/acquisition` page joins against the task
 * list to render "task X is running on edge Y" — it is NOT the InfluxDB
 * polling path.
 */
export async function listTaskStatuses(
  filters?: TaskStatusFilters,
  signal?: AbortSignal,
): Promise<EdgeTaskStatus[]> {
  return fetchAllPages<EdgeTaskStatus>(async (limit, offset) => {
    const res = await apiClient.get('/fleet/task-statuses/', {
      params: { limit, offset, task: filters?.task, edge: filters?.edge },
      signal,
    });
    return res.data;
  });
}
