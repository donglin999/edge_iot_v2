/**
 * API service for device detail page
 */
import { fetchAllPages, withLimitOffset } from './pagination';

const API_BASE = '/api/config';

/**
 * 设备状态,由后端从连接告警 + 运行中的会话推出来。
 *
 * - `online`  在跑,且没有未清除的连接告警
 * - `offline` 有未清除的连接告警 —— 采集在跑但连不上
 * - `idle`    不在任何运行中的会话里 —— 系统压根没在连它
 *
 * 后端不做主动探测,所以 `idle` 的含义是「不知道」而不是「连得上」。
 */
export type DeviceStatus = 'online' | 'offline' | 'idle';

export interface Device {
  id: number;
  site: number;
  site_code?: string;
  protocol: string;
  ip_address: string;
  port: number | null;
  name: string;
  code: string;
  status?: DeviceStatus;
  created_at: string;
  updated_at: string;
}

export interface Point {
  id: number;
  device: number;
  channel: number | null;
  template: number | null;
  code: string;
  address: string;
  description: string;
  sample_rate_hz: number;
  to_kafka: boolean;
  extra: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface DeviceStats {
  total_points: number;
  task_count: number;
  last_acquisition: string | null;
  related_tasks: Array<{
    id: number;
    code: string;
    name: string;
    is_active: boolean;
  }>;
}

export interface ConnectionTestResult {
  success: boolean;
  message: string;
  details: Record<string, unknown>;
}

export interface DeviceLatestPoint {
  point_code: string;
  point_name: string;
  unit: string;
  data_type: string;
  value: number | string | boolean | null;
  quality: string;
  timestamp: string | null;
}

export interface DeviceLatestValues {
  device_id: number;
  device_code: string;
  device_name: string;
  protocol: string;
  online: boolean;
  last_activity_at: string | null;
  points: DeviceLatestPoint[];
}

/**
 * 获取所有设备列表。
 *
 * `/devices/` 是标准 list 端点,DRF 全局分页后返回 `{ results }`;
 * 这里逐页抓取并合并,调用方仍拿到完整数组(XIU-9 / H10)。
 */
export async function fetchDevices(): Promise<Device[]> {
  return fetchAllPages<Device>(async (limit, offset) => {
    const response = await fetch(withLimitOffset(`${API_BASE}/devices/`, limit, offset));
    if (!response.ok) {
      throw new Error(`获取设备列表失败: ${response.statusText}`);
    }
    return response.json();
  });
}

/**
 * 获取设备详情
 */
export async function fetchDevice(deviceId: number): Promise<Device> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/`);
  if (!response.ok) {
    throw new Error(`获取设备详情失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 获取设备的所有测点
 */
export async function fetchDevicePoints(deviceId: number): Promise<Point[]> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/points/`);
  if (!response.ok) {
    throw new Error(`获取设备测点失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 获取设备统计信息
 */
export async function fetchDeviceStats(deviceId: number): Promise<DeviceStats> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/stats/`);
  if (!response.ok) {
    throw new Error(`获取设备统计失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 测试设备连接
 */
export async function testDeviceConnection(deviceId: number): Promise<ConnectionTestResult> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/test-connection/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || `连接测试失败: ${response.statusText}`);
  }
  return data;
}

/**
 * 更新设备信息
 */
export async function updateDevice(deviceId: number, data: Partial<Device>): Promise<Device> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    throw new Error(`更新设备失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 获取设备所有测点的最新值（含设备元信息和在线状态）
 */
export async function fetchDeviceLatestValues(
  deviceId: number
): Promise<DeviceLatestValues> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/latest-values/`);
  if (!response.ok) {
    throw new Error(`获取设备最新值失败: ${response.statusText}`);
  }
  return response.json();
}

/**
 * 删除设备
 */
export async function deleteDevice(deviceId: number): Promise<void> {
  const response = await fetch(`${API_BASE}/devices/${deviceId}/`, {
    method: 'DELETE',
  });

  if (!response.ok) {
    throw new Error(`删除设备失败: ${response.statusText}`);
  }
}
