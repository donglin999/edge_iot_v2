/**
 * API service for configuration version management
 */

const API_BASE = '/api/config';

export interface ConfigVersion {
  id: number;
  task: number;
  task_code?: string;
  version: number;
  summary: string;
  created_by: string;
  payload: {
    device: string;
    points: Array<{
      code: string;
      address: string;
      description: string;
      sample_rate_hz: number;
    }>;
  };
  created_at: string;
  updated_at: string;
}

export interface RollbackResponse {
  detail: string;
  new_version_id: number;
  new_version_number: number;
  rollback_from_version: number;
}

/**
 * Fetch all versions for a specific task
 */
export async function fetchTaskVersions(taskId: number): Promise<ConfigVersion[]> {
  const response = await fetch(`${API_BASE}/versions/?task_id=${taskId}`);
  if (!response.ok) {
    throw new Error(`获取版本列表失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * Fetch a specific version by ID
 */
export async function fetchVersion(versionId: number): Promise<ConfigVersion> {
  const response = await fetch(`${API_BASE}/versions/${versionId}/`);
  if (!response.ok) {
    throw new Error(`获取版本详情失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * Rollback to a specific version
 */
export async function rollbackToVersion(versionId: number): Promise<RollbackResponse> {
  const response = await fetch(`${API_BASE}/versions/${versionId}/rollback/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || `回滚失败: ${response.statusText}`);
  }
  return data;
}

/**
 * Fetch all tasks for version history selection
 */
export async function fetchAllTasks(): Promise<Array<{ id: number; code: string; name: string }>> {
  const response = await fetch(`${API_BASE}/tasks/`);
  if (!response.ok) {
    throw new Error(`获取任务列表失败: ${response.statusText}`);
  }
  return response.json();
}
