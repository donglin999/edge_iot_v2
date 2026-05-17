/**
 * API service for configuration version management.
 *
 * Adds export / delete / bulk-delete on top of the existing list+rollback
 * helpers consumed by VersionHistoryPage.
 */
import { fetchAllPages, withLimitOffset } from './pagination';

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

export interface Site {
  id: number;
  code: string;
  name: string;
  description?: string;
  created_at?: string;
  updated_at?: string;
}

export interface BulkDeleteResponse {
  deleted: number[];
  skipped: Array<{ id: number; reason: string }>;
}

/**
 * Fetch versions. If taskId is omitted, the backend returns all versions
 * across tasks (newest first).
 */
export async function fetchTaskVersions(taskId?: number | null): Promise<ConfigVersion[]> {
  // 标准 list 端点:DRF 全局分页后逐页合并(XIU-9 / H10)。
  const base =
    taskId === undefined || taskId === null
      ? `${API_BASE}/versions/`
      : `${API_BASE}/versions/?task_id=${taskId}`;
  return fetchAllPages<ConfigVersion>(async (limit, offset) => {
    const response = await fetch(withLimitOffset(base, limit, offset));
    if (!response.ok) {
      throw new Error(`获取版本列表失败: ${response.statusText}`);
    }
    return response.json();
  });
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
  // 标准 list 端点:DRF 全局分页后逐页合并(XIU-9 / H10)。
  return fetchAllPages<{ id: number; code: string; name: string }>(async (limit, offset) => {
    const response = await fetch(withLimitOffset(`${API_BASE}/tasks/`, limit, offset));
    if (!response.ok) {
      throw new Error(`获取任务列表失败: ${response.statusText}`);
    }
    return response.json();
  });
}

/**
 * Fetch all sites (used by "导出当前配置" picker).
 */
export async function fetchSites(): Promise<Site[]> {
  // 标准 list 端点:DRF 全局分页后逐页合并(XIU-9 / H10)。
  return fetchAllPages<Site>(async (limit, offset) => {
    const response = await fetch(withLimitOffset(`${API_BASE}/sites/`, limit, offset));
    if (!response.ok) {
      throw new Error(`获取站点列表失败: ${response.statusText}`);
    }
    return response.json();
  });
}

/** Pull a filename out of a Content-Disposition header, falling back to a default. */
function pickFilename(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback;
  // Try RFC 5987 filename* first, then plain filename=
  const utf = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf) {
    try {
      return decodeURIComponent(utf[1]);
    } catch {
      // ignore decoding error and fall through
    }
  }
  const simple = disposition.match(/filename="?([^";]+)"?/i);
  return simple ? simple[1] : fallback;
}

async function downloadXlsx(url: string, fallback: string): Promise<void> {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      // body is not JSON (likely HTML / empty); keep statusText
    }
    throw new Error(detail || `下载失败: ${res.status}`);
  }
  const blob = await res.blob();
  const filename = pickFilename(res.headers.get('Content-Disposition'), fallback);
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(objectUrl);
}

/**
 * Export the current site configuration as an Excel file.
 */
export async function exportCurrentConfig(siteCode: string = 'default'): Promise<void> {
  const url = `${API_BASE}/export-excel/?site_code=${encodeURIComponent(siteCode)}`;
  await downloadXlsx(url, `config_${siteCode}_${Date.now()}.xlsx`);
}

/**
 * Export a specific version's payload as an Excel file.
 */
export async function exportVersion(versionId: number): Promise<void> {
  const url = `${API_BASE}/versions/${versionId}/export-excel/`;
  await downloadXlsx(url, `version_${versionId}_${Date.now()}.xlsx`);
}

/**
 * Delete a single version. Backend may reject with 400 + {detail: ...}
 * if the underlying task is running.
 */
export async function deleteVersion(versionId: number): Promise<void> {
  const response = await fetch(`${API_BASE}/versions/${versionId}/`, {
    method: 'DELETE',
  });
  if (response.status === 204) return;
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = data.detail || detail;
    } catch {
      // ignore
    }
    throw new Error(detail || `删除失败: ${response.status}`);
  }
}

/**
 * Bulk-delete versions. Backend returns 200 with per-id outcome even when
 * some entries are skipped due to running tasks.
 */
export async function bulkDeleteVersions(ids: number[]): Promise<BulkDeleteResponse> {
  const response = await fetch(`${API_BASE}/versions/bulk-delete/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `批量删除失败: ${response.statusText}`);
  }
  return {
    deleted: Array.isArray(data.deleted) ? data.deleted : [],
    skipped: Array.isArray(data.skipped) ? data.skipped : [],
  };
}
