/**
 * API service for data visualization.
 *
 * Every request accepts an optional `AbortSignal` (threaded through
 * `fetchWithAbort`) so callers can cancel in-flight requests from a
 * `useEffect` cleanup — see H12 in XIU-7.
 */
import { fetchWithAbort } from './http';

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
 * Fetch point history data for visualization
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
  const response = await fetchWithAbort(
    `/api/acquisition/sessions/?limit=${limit}`,
    signal
  );

  if (!response.ok) {
    throw new Error(`获取会话列表失败: ${response.statusText}`);
  }

  const data = await response.json();
  return data.results || data;
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
