/**
 * Acquisition API Service
 * 采集任务控制 API 接口封装
 *
 * 所有请求均通过 `fetchWithAbort` 透传可选的 `AbortSignal`，
 * 以便调用方在 `useEffect` cleanup 中取消未完成的请求（XIU-7 H12）。
 */
import { fetchWithAbort } from './http';
import { fetchAllPages, unwrapList, withLimitOffset } from './pagination';

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
  sample_rate_hz: number;
  /** 任务绑定设备的协议(一任务一设备);无测点时为 null。scada/mqtt 为推模式,无采集频率概念。 */
  device_protocol?: string | null;
  created_at: string;
  updated_at: string;
}

/** 推模式协议:数据由对端推送,拿到即消费,「采样频率」概念不适用。 */
export const PUSH_PROTOCOLS = ['scada', 'mqtt'];

export interface StartTaskRequest {
  task_id: number;
  config_version_id?: number;
  worker_identifier?: string;
  metadata?: Record<string, unknown>;
}

export interface DeviceValidationResult {
  status: 'healthy' | 'partial' | 'error';
  connected: boolean;
  total_points: number;
  successful_points?: number;
  failed_points?: number;
  error?: string;
}

export interface StartTaskValidation {
  all_healthy: boolean;
  total_points: number;
  failed_points_count: number;
  device_results: Record<string, DeviceValidationResult>;
  failed_points_sample?: Array<{ device: string; point: string; reason: string }>;
}

export interface StartTaskResponse {
  detail?: string;
  session_id?: number;
  celery_task_id?: string;
  task_id?: number;
  task_code?: string;
  message?: string;
  validation?: StartTaskValidation;
  elapsed_seconds?: number;
}

const API_BASE = '/api';

/**
 * 获取所有采集任务列表
 */
export async function fetchTasks(signal?: AbortSignal): Promise<AcqTask[]> {
  // 标准 list 端点:DRF 全局分页(limit/offset)后逐页合并(XIU-9 / H10)。
  return fetchAllPages<AcqTask>(async (limit, offset) => {
    const response = await fetchWithAbort(
      withLimitOffset(`${API_BASE}/config/tasks/`, limit, offset),
      signal
    );
    if (!response.ok) {
      throw new Error(`获取任务列表失败: ${response.statusText}`);
    }
    return response.json();
  });
}

/**
 * 获取活跃的采集会话列表
 */
export async function fetchActiveSessions(
  signal?: AbortSignal
): Promise<AcquisitionSession[]> {
  const response = await fetchWithAbort(
    `${API_BASE}/acquisition/sessions/active/`,
    signal
  );
  if (!response.ok) {
    throw new Error(`获取活跃会话失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 获取所有采集会话历史
 */
export async function fetchSessions(
  limit = 20,
  signal?: AbortSignal
): Promise<AcquisitionSession[]> {
  // 标准 list 端点:DRF 全局分页后返回 `{ results }`。直接用 `?limit=N`
  // 取最近 N 条即可满足"最近会话历史"场景(XIU-9 / H10)。
  const response = await fetchWithAbort(
    withLimitOffset(`${API_BASE}/acquisition/sessions/`, limit, 0),
    signal
  );
  if (!response.ok) {
    throw new Error(`获取会话历史失败: ${response.statusText}`);
  }
  return unwrapList<AcquisitionSession>(await response.json());
}

/**
 * 获取指定会话的状态详情
 */
export async function fetchSessionStatus(
  sessionId: number,
  signal?: AbortSignal
): Promise<SessionStatus> {
  const response = await fetchWithAbort(
    `${API_BASE}/acquisition/sessions/${sessionId}/status/`,
    signal
  );
  if (!response.ok) {
    throw new Error(`获取会话状态失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 启动采集任务
 */
export async function startTask(
  request: StartTaskRequest,
  signal?: AbortSignal
): Promise<StartTaskResponse> {
  const response = await fetchWithAbort(
    `${API_BASE}/acquisition/sessions/start-task/`,
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(request),
    }
  );

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
  reason?: string,
  signal?: AbortSignal
): Promise<{ detail: string; session_id: number; current_status: string }> {
  const response = await fetchWithAbort(
    `${API_BASE}/acquisition/sessions/${sessionId}/stop/`,
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ reason }),
    }
  );

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `停止会话失败: ${response.statusText}`);
  }

  return data;
}

/**
 * 通过配置API启动任务（兼容旧接口）
 */
export async function startTaskViaConfig(
  taskId: number,
  signal?: AbortSignal
): Promise<unknown> {
  const response = await fetchWithAbort(
    `${API_BASE}/config/tasks/${taskId}/start/`,
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({}),
    }
  );

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `启动任务失败: ${response.statusText}`);
  }

  return data;
}

/**
 * 通过配置API停止任务（兼容旧接口）
 */
export async function stopTaskViaConfig(
  taskId: number,
  signal?: AbortSignal
): Promise<unknown> {
  const response = await fetchWithAbort(
    `${API_BASE}/config/tasks/${taskId}/stop/`,
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({}),
    }
  );

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `停止任务失败: ${response.statusText}`);
  }

  return data;
}

/**
 * 更新任务采样频率（Hz），后端会在保存后自动重启 running 会话
 */
export async function updateTaskSampleRate(
  taskId: number,
  rateHz: number,
  signal?: AbortSignal
): Promise<AcqTask> {
  const response = await fetchWithAbort(
    `${API_BASE}/config/tasks/${taskId}/`,
    signal,
    {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ sample_rate_hz: rateHz }),
    }
  );

  const data = await response.json();

  if (!response.ok) {
    const detail =
      (data && (data.detail || data.sample_rate_hz)) ||
      `更新采样频率失败: ${response.statusText}`;
    throw new Error(
      Array.isArray(detail) ? detail.join('; ') : String(detail)
    );
  }

  return data as AcqTask;
}

/**
 * 测试单次采集
 */
export async function testAcquire(
  taskId: number,
  signal?: AbortSignal
): Promise<unknown> {
  const response = await fetchWithAbort(
    `${API_BASE}/acquisition/sessions/test-acquire/`,
    signal,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ task_id: taskId }),
    }
  );

  const data = await response.json();

  if (!response.ok) {
    throw new Error(data.detail || `测试采集失败: ${response.statusText}`);
  }

  return data;
}
