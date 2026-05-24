/**
 * API service for data visualization.
 *
 * Every request accepts an optional `AbortSignal` (threaded through
 * `fetchWithAbort`) so callers can cancel in-flight requests from a
 * `useEffect` cleanup — see H12 in XIU-7.
 */
import { fetchWithAbort } from './http';
import { unwrapList, withLimitOffset } from './pagination';

export interface PointLatestValue {
  point_code: string;
  point_name: string;
  unit: string;
  data_type: string;
  device_id: number;
  device_name: string;
  value: number | string | boolean | null;
  quality: string;
  timestamp: string | null;
}

export interface PointsLatestValuesFilter {
  taskId?: number | null;
  deviceId?: number | null;
  pointCode?: string | null;
}

export interface PointsLatestValuesResponse {
  filter: {
    task_id: number | null;
    device_id: number | null;
    point_code: string | null;
  };
  count: number;
  points: PointLatestValue[];
}

/**
 * Fetch the latest value for every point matching the given (task / device / point) filter.
 * Backed by `GET /api/config/points/latest-values/`.
 */
export async function fetchPointsLatestValues(
  filter: PointsLatestValuesFilter = {},
  signal?: AbortSignal
): Promise<PointsLatestValuesResponse> {
  const params = new URLSearchParams();
  if (filter.taskId !== undefined && filter.taskId !== null) {
    params.set('task_id', String(filter.taskId));
  }
  if (filter.deviceId !== undefined && filter.deviceId !== null) {
    params.set('device_id', String(filter.deviceId));
  }
  if (filter.pointCode) {
    params.set('point_code', filter.pointCode);
  }
  const qs = params.toString();
  const url = `/api/config/points/latest-values/${qs ? `?${qs}` : ''}`;
  const response = await fetchWithAbort(url, signal);
  if (!response.ok) {
    throw new Error(`获取测点最新值失败: ${response.statusText}`);
  }
  return response.json();
}

export interface DataPoint {
  timestamp: string;
  value: number | string | boolean;
  quality: string;
}

export interface PointHistoryResponse {
  point_code: string;
  start_time: string | null;
  end_time: string | null;
  count: number;
  data: DataPoint[];
}

export interface AcquisitionSession {
  id: number;
  task: number;
  task_code: string;
  task_name: string;
  status: string;
  celery_task_id: string;
  started_at: string | null;
  stopped_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionDataPoint {
  id: number;
  session: number;
  point_code: string;
  timestamp: string;
  value: number | string | boolean;
  quality: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface SessionDataPointsResponse {
  count: number;
  results: SessionDataPoint[];
}

/**
 * M6 history-proxy response shape (XIU-83).
 *
 * The center fans out one HTTP call per edge and merges the results;
 * `sources[edge_id]` exposes the per-edge payload so the UI can show
 * "数据来源: edge X", and `errors[edge_id]` carries explicit per-edge
 * failures (offline, timeout, unreachable) so the Drawer can render a
 * clear error instead of an empty chart.
 *
 * `data` is the merged stream with `edge_id` stamped on every row.
 */
export interface HistoryPointsSource {
  edge_id: string | null;
  point_ids?: string[];
  count: number;
  truncated?: boolean;
  elapsed_ms?: number;
  data: HistoryPointSample[];
}

export interface HistoryPointSample {
  point_code: string;
  timestamp: string;
  value: number | string | boolean | null;
  quality: string;
  edge_id?: string | null;
}

export interface HistoryPointsError {
  edge_id: string | null;
  status: number;
  code: string;
  message: string;
}

export interface HistoryPointsResponse {
  task_ids: number[];
  point_ids: string[];
  start: string;
  end: string;
  agg: string;
  limit: number;
  count: number;
  queried_at: string;
  sources: Record<string, HistoryPointsSource>;
  errors: Record<string, HistoryPointsError>;
  data: HistoryPointSample[];
}

export interface FetchHistoryPointsOptions {
  taskIds: number[];
  pointIds: string[];
  start?: string;
  end?: string;
  agg?: 'raw' | '1s' | '10s';
  limit?: number;
}

/**
 * Fetch history samples for one or more points via the M6 proxy.
 *
 * Backed by `GET /api/history/points`. The center resolves each task to
 * its owning edge, calls the edge's read-only `/history/points`, and
 * returns merged results. Edge-offline shows up as a 503 (no successful
 * source) or as a per-source entry in `errors` (partial success).
 */
export async function fetchHistoryPoints(
  options: FetchHistoryPointsOptions,
  signal?: AbortSignal
): Promise<HistoryPointsResponse> {
  const params = new URLSearchParams();
  params.set('task_id', options.taskIds.join(','));
  params.set('point_ids', options.pointIds.join(','));
  if (options.start) params.set('start', options.start);
  if (options.end) params.set('end', options.end);
  if (options.agg) params.set('agg', options.agg);
  if (options.limit !== undefined) params.set('limit', String(options.limit));

  const response = await fetchWithAbort(
    `/api/history/points?${params.toString()}`,
    signal
  );

  // 503 from the proxy = every targeted edge failed; we still want the
  // body so callers can render the per-edge error reason.
  if (!response.ok && response.status !== 503) {
    throw new Error(`获取历史数据失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * Fetch point history data for visualization (legacy single-host route).
 *
 * Retained for callers that don't have a `task_id` handy (e.g. CSV
 * export from the realtime panel). New code should prefer
 * `fetchHistoryPoints` so the request is dispatched to the right edge.
 */
export async function fetchPointHistory(
  pointCode: string,
  startTime?: string,
  endTime?: string,
  limit: number = 1000,
  signal?: AbortSignal
): Promise<PointHistoryResponse> {
  const params = new URLSearchParams({
    point_code: pointCode,
    limit: limit.toString(),
  });

  if (startTime) {
    params.append('start_time', startTime);
  }

  if (endTime) {
    params.append('end_time', endTime);
  }

  const response = await fetchWithAbort(
    `/api/acquisition/sessions/point-history/?${params.toString()}`,
    signal
  );

  if (!response.ok) {
    throw new Error(`获取测点历史数据失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch active acquisition sessions
 */
export async function fetchActiveSessions(
  signal?: AbortSignal
): Promise<AcquisitionSession[]> {
  const response = await fetchWithAbort('/api/acquisition/sessions/active/', signal);

  if (!response.ok) {
    throw new Error(`获取活跃会话失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch all acquisition sessions
 */
export async function fetchSessions(
  limit: number = 50,
  signal?: AbortSignal
): Promise<AcquisitionSession[]> {
  // 标准 list 端点:DRF 全局分页后返回 `{ results }`;用 `?limit=N` 取最近
  // N 条(XIU-9 / H10)。
  const response = await fetchWithAbort(
    withLimitOffset('/api/acquisition/sessions/', limit, 0),
    signal
  );

  if (!response.ok) {
    throw new Error(`获取会话列表失败: ${response.statusText}`);
  }

  return unwrapList<AcquisitionSession>(await response.json());
}

/**
 * Fetch session details
 */
export async function fetchSession(
  sessionId: number,
  signal?: AbortSignal
): Promise<AcquisitionSession> {
  const response = await fetchWithAbort(
    `/api/acquisition/sessions/${sessionId}/`,
    signal
  );

  if (!response.ok) {
    throw new Error(`获取会话详情失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch data points for a session
 */
export async function fetchSessionDataPoints(
  sessionId: number,
  limit: number = 100,
  offset: number = 0,
  signal?: AbortSignal
): Promise<SessionDataPointsResponse> {
  const response = await fetchWithAbort(
    `/api/acquisition/sessions/${sessionId}/data-points/?limit=${limit}&offset=${offset}`,
    signal
  );

  if (!response.ok) {
    throw new Error(`获取会话数据点失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Start an acquisition task
 */
export async function startTask(
  taskId: number,
  signal?: AbortSignal
): Promise<AcquisitionSession> {
  const response = await fetchWithAbort(
    '/api/acquisition/sessions/start-task/',
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ task_id: taskId }),
    }
  );

  if (!response.ok) {
    const errorData = await response.json();
    throw new Error(errorData.detail || `启动任务失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Stop an acquisition session
 */
export async function stopSession(
  sessionId: number,
  reason?: string,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetchWithAbort(
    `/api/acquisition/sessions/${sessionId}/stop/`,
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ reason: reason || '手动停止' }),
    }
  );

  if (!response.ok) {
    const errorData = await response.json();
    throw new Error(errorData.detail || `停止会话失败: ${response.statusText}`);
  }
}
