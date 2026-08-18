/**
 * 采集任务 + 测点的增删改。
 *
 * 采集任务 = 一组测点 + 采样频率,是「启停采集」的单位。以前只能从设备导入时
 * 自动生成、在采集控制页启停,没有任何地方能手动新建/改测点/删除 —— 这里补上
 * 全套 CRUD,配合采集控制页把「任务管理」并进去。
 *
 * 测点(Point)归属于设备(Device),任务只是引用一组测点。所以「给任务加测点」
 * 有两条路:选设备上已有的测点、或就地在设备上新建一个测点再纳入。
 */
import { apiClient } from './apiClient';

/** 设备下的一个测点(与 /config/points/ 序列化一致)。 */
export interface TaskPoint {
  id: number;
  device: number;
  code: string;
  address: string;
  description: string;
  sample_rate_hz: number;
  extra: Record<string, unknown>;
}

export interface TaskDetail {
  id: number;
  code: string;
  name: string;
  description: string;
  sample_rate_hz: number;
  is_active: boolean;
  /** 任务包含的测点 id 列表。 */
  points: number[];
}

export interface TaskWritePayload {
  code?: string;
  name?: string;
  description?: string;
  sample_rate_hz?: number;
  is_active?: boolean;
  points?: number[];
}

export interface PointWritePayload {
  device: number;
  code: string;
  address?: string;
  description?: string;
  sample_rate_hz?: number;
  extra?: Record<string, unknown>;
}

// ── 任务 ────────────────────────────────────────────────────────────────

export async function fetchTask(id: number): Promise<TaskDetail> {
  const res = await apiClient.get<TaskDetail>(`/config/tasks/${id}/`);
  return res.data;
}

export async function createTask(payload: TaskWritePayload): Promise<TaskDetail> {
  const res = await apiClient.post<TaskDetail>('/config/tasks/', payload);
  return res.data;
}

export async function updateTask(id: number, payload: TaskWritePayload): Promise<TaskDetail> {
  const res = await apiClient.patch<TaskDetail>(`/config/tasks/${id}/`, payload);
  return res.data;
}

export async function deleteTask(id: number): Promise<void> {
  await apiClient.delete(`/config/tasks/${id}/`);
}

// ── 测点 ────────────────────────────────────────────────────────────────

/** 某设备下的全部测点。 */
export async function fetchDevicePoints(deviceId: number): Promise<TaskPoint[]> {
  const res = await apiClient.get(`/config/devices/${deviceId}/points/`);
  // 该端点直接返回数组(非分页)。
  return Array.isArray(res.data) ? res.data : res.data.results ?? [];
}

export async function createPoint(payload: PointWritePayload): Promise<TaskPoint> {
  const res = await apiClient.post<TaskPoint>('/config/points/', payload);
  return res.data;
}

export async function updatePoint(
  id: number,
  payload: Partial<PointWritePayload>,
): Promise<TaskPoint> {
  const res = await apiClient.patch<TaskPoint>(`/config/points/${id}/`, payload);
  return res.data;
}

export async function deletePoint(id: number): Promise<void> {
  await apiClient.delete(`/config/points/${id}/`);
}
