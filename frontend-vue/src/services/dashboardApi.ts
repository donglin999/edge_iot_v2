import { fetchWithAbort } from './http';
import { fetchAllPages, withLimitOffset } from './pagination';

export interface DashboardTaskRun {
  task: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  worker: string | null;
  log_reference: string | null;
}

export interface DashboardOverview {
  total_tasks: number;
  active_tasks: number;
  status: Record<string, number>;
  recent_runs: DashboardTaskRun[];
  generated_at: string;
}

export interface DashboardTask {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

export interface DashboardDevice {
  id: number;
  status?: 'online' | 'offline' | string;
}

export interface DashboardSession {
  id: number;
  status: string;
}

async function readJson<T>(response: Response, label: string): Promise<T> {
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${label}失败: ${detail || response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export async function fetchDashboardOverview(
  signal?: AbortSignal,
): Promise<DashboardOverview> {
  const response = await fetchWithAbort(
    '/api/config/tasks/overview/?site_code=default',
    signal,
  );
  return readJson<DashboardOverview>(response, '获取任务概览');
}

export async function fetchDashboardTasks(
  signal?: AbortSignal,
): Promise<DashboardTask[]> {
  return fetchAllPages<DashboardTask>(async (limit, offset) => {
    const response = await fetchWithAbort(
      withLimitOffset('/api/config/tasks/?site_code=default', limit, offset),
      signal,
    );
    return readJson<unknown>(response, '获取任务列表');
  });
}

export async function fetchDashboardDevices(
  signal?: AbortSignal,
): Promise<DashboardDevice[]> {
  return fetchAllPages<DashboardDevice>(async (limit, offset) => {
    const response = await fetchWithAbort(
      withLimitOffset('/api/config/devices/', limit, offset),
      signal,
    );
    return readJson<unknown>(response, '获取设备列表');
  });
}

export async function fetchDashboardActiveSessions(
  signal?: AbortSignal,
): Promise<DashboardSession[]> {
  const response = await fetchWithAbort(
    '/api/acquisition/sessions/active/',
    signal,
  );
  return readJson<DashboardSession[]>(response, '获取活跃会话');
}
