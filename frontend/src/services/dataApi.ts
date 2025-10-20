/**
 * API service for data visualization
 */

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
  limit: number = 1000
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

  const response = await fetch(
    `/api/acquisition/sessions/point-history/?${params.toString()}`
  );

  if (!response.ok) {
    throw new Error(`获取测点历史数据失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch active acquisition sessions
 */
export async function fetchActiveSessions(): Promise<AcquisitionSession[]> {
  const response = await fetch('/api/acquisition/sessions/active/');

  if (!response.ok) {
    throw new Error(`获取活跃会话失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch all acquisition sessions
 */
export async function fetchSessions(limit: number = 50): Promise<AcquisitionSession[]> {
  const response = await fetch(`/api/acquisition/sessions/?limit=${limit}`);

  if (!response.ok) {
    throw new Error(`获取会话列表失败: ${response.statusText}`);
  }

  const data = await response.json();
  return data.results || data;
}

/**
 * Fetch session details
 */
export async function fetchSession(sessionId: number): Promise<AcquisitionSession> {
  const response = await fetch(`/api/acquisition/sessions/${sessionId}/`);

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
  offset: number = 0
): Promise<SessionDataPointsResponse> {
  const response = await fetch(
    `/api/acquisition/sessions/${sessionId}/data-points/?limit=${limit}&offset=${offset}`
  );

  if (!response.ok) {
    throw new Error(`获取会话数据点失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Start an acquisition task
 */
export async function startTask(taskId: number): Promise<AcquisitionSession> {
  const response = await fetch('/api/acquisition/sessions/start-task/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ task_id: taskId }),
  });

  if (!response.ok) {
    const errorData = await response.json();
    throw new Error(errorData.detail || `启动任务失败: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Stop an acquisition session
 */
export async function stopSession(sessionId: number, reason?: string): Promise<void> {
  const response = await fetch(`/api/acquisition/sessions/${sessionId}/stop/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ reason: reason || '手动停止' }),
  });

  if (!response.ok) {
    const errorData = await response.json();
    throw new Error(errorData.detail || `停止会话失败: ${response.statusText}`);
  }
}
