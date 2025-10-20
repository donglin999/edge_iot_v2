/**
 * API service for device detail page
 */

const API_BASE = '/api/config';

export interface Device {
  id: number;
  site: number;
  site_code?: string;
  protocol: string;
  ip_address: string;
  port: number | null;
  name: string;
  code: string;
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

/**
 * 获取所有设备列表
 */
export async function fetchDevices(): Promise<Device[]> {
  const response = await fetch(`${API_BASE}/devices/`);
  if (!response.ok) {
    throw new Error(`获取设备列表失败: ${response.statusText}`);
  }
  return response.json();
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
