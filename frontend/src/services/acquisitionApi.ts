/**
 * Acquisition API Service
 * 采集任务控制 API 接口封装
 */

export interface AcquisitionSession {
  id: number;
  task: number;
  task_code: string;
  task_name: string;
  worker: number | null;
  worker_identifier: string | null;
  status: 'starting' | 'running' | 'paused' | 'stopping' | 'stopped' | 'error';
  celery_task_id: string;
  pid: number | null;
  started_at: string | null;
  stopped_at: string | null;
  duration_seconds: number | null;
  error_message: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SessionStatus {
  session_id: number;
  task_code: string;
  task_name: string;
  status: string;
  celery_task_id: string | null;
  started_at: string | null;
  stopped_at: string | null;
  duration_seconds: number | null;
  points_read: number;
  last_read_time: string | null;
  error_count: number;
  error_message: string;
  metadata: Record<string, unknown>;
}

export interface AcqTask {
  id: number;
  code: string;
  name: string;
  description: string;
  schedule: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface StartTaskRequest {
  task_id: number;
  config_version_id?: number;
  worker_identifier?: string;
  metadata?: Record<string, unknown>;
}

export interface StartTaskResponse {
  detail?: string;
  session_id?: number;
  celery_task_id?: string;
  task_id?: number;
  task_code?: string;
  message?: string;
}

const API_BASE = '/api';

/**
 * 获取所有采集任务列表
 */
export async function fetchTasks(): Promise<AcqTask[]> {
  const response = await fetch(`${API_BASE}/config/tasks/`);
  if (!response.ok) {
    throw new Error(`获取任务列表失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 获取活跃的采集会话列表
 */
export async function fetchActiveSessions(): Promise<AcquisitionSession[]> {
  const response = await fetch(`${API_BASE}/acquisition/sessions/active/`);
  if (!response.ok) {
    throw new Error(`获取活跃会话失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 获取所有采集会话历史
 */
export async function fetchSessions(limit = 20): Promise<AcquisitionSession[]> {
  const response = await fetch(`${API_BASE}/acquisition/sessions/?limit=${limit}`);
  if (!response.ok) {
    throw new Error(`获取会话历史失败: ${response.statusText}`);
  }
  const data = await response.json();
  return data.results || data;
}

/**
 * 获取指定会话的状态详情
 */
export async function fetchSessionStatus(sessionId: number): Promise<SessionStatus> {
  const response = await fetch(`${API_BASE}/acquisition/sessions/${sessionId}/status/`);
  if (!response.ok) {
    throw new Error(`获取会话状态失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 启动采集任务
 */
export async function startTask(request: StartTaskRequest): Promise<StartTaskResponse> {
  const response = await fetch(`${API_BASE}/acquisition/sessions/start-task/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `启动任务失败: ${response.statusText}`);
  }

  return data;
}

/**
 * 停止采集会话
 */
export async function stopSession(
  sessionId: number,
  reason?: string
): Promise<{ detail: string; session_id: number; current_status: string }> {
  const response = await fetch(`${API_BASE}/acquisition/sessions/${sessionId}/stop/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ reason }),
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `停止会话失败: ${response.statusText}`);
  }

  return data;
}

/**
 * 通过配置API启动任务（兼容旧接口）
 */
export async function startTaskViaConfig(taskId: number): Promise<unknown> {
  const response = await fetch(`${API_BASE}/config/tasks/${taskId}/start/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({}),
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `启动任务失败: ${response.statusText}`);
  }
 
  return data;
}

/**
 * 通过配置API停止任务（兼容旧接口）
 */
export async function stopTaskViaConfig(taskId: number): Promise<unknown> {
  const response = await fetch(`${API_BASE}/config/tasks/${taskId}/stop/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({}),
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `停止任务失败: ${response.statusText}`);
  }

  return data;
}

/**
 * 测试单次采集
 */
export async function testAcquire(taskId: number): Promise<unknown> {
  const response = await fetch(`${API_BASE}/acquisition/sessions/test-acquire/`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ task_id: taskId }),
  });

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `测试采集失败: ${response.statusText}`);
  }

  return data;
}
