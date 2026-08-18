/**
 * SCADA 网关 API.
 *
 * 背景:scada 协议(中山小家电注塑机 MQTT SCADA 网关)下,每台设备都要重复
 * 一整套 MQTT 连接参数(broker/端口/TLS/账号/密码/qos/product_key/话题模板),
 * 真正随设备变化的只有 `device_name` 和测点 `code`。
 *
 * 网关把这套连接参数收敛成一份共享配置:服务配一次,之后加设备只填
 * `device_name`,加测点只填 `code` + 中文名 + 类型;`provision` 一次性把
 * 设备 / 测点 / 采集任务批量落库(幂等,重复 provision 是更新而非重复创建)。
 */
import type { AxiosRequestConfig } from 'axios';

import { apiClient, downloadFile } from './apiClient';
import { fetchAllPages } from './pagination';

/** 测点数据类型(与后端 Point.data_type 取值一致)。 */
export type ScadaDataType = 'string' | 'int' | 'float' | 'bool';

export const SCADA_DATA_TYPES: ScadaDataType[] = ['float', 'int', 'bool', 'string'];

/** 网关服务配置——所有设备共享的那份 MQTT 连接参数。 */
export interface ScadaGateway {
  id: number;
  code: string;
  name: string;
  source_ip: string;
  source_port: number;
  mqtt_use_tls: boolean;
  mqtt_username: string;
  mqtt_password: string;
  mqtt_qos: number;
  mqtt_client_id: string;
  mqtt_read_timeout: number;
  product_key: string;
  topic_template: string;
  /** 只读:后端统计的挂在该网关下的设备数。 */
  device_count: number;
  created_at: string;
  updated_at: string;
}

/** 创建 / 更新网关时可写的字段(server 侧只读字段除外)。 */
export type ScadaGatewayInput = Omit<
  ScadaGateway,
  'id' | 'device_count' | 'created_at' | 'updated_at'
>;

export interface ProvisionPointInput {
  code: string;
  description?: string;
  data_type?: ScadaDataType;
  unit?: string;
}

export interface ProvisionDeviceInput {
  /** SCADA 侧设备名,话题中的 {device_name} 段。 */
  device_name: string;
  /** 可选友好名;留空时后端回落到 device_name。 */
  name?: string;
  points: ProvisionPointInput[];
}

export interface ProvisionTaskInput {
  code: string;
  name: string;
  sample_rate_hz: number;
  is_active: boolean;
}

export interface ProvisionPayload {
  /** 可选;不传时由后端决定归属站点。 */
  site?: number;
  devices: ProvisionDeviceInput[];
  /** 可选;传了才会顺带建/更新采集任务。 */
  task?: ProvisionTaskInput;
}

export interface ProvisionedDevice {
  id: number;
  device_name: string;
  code: string;
  points: Array<{ id: number; code: string }>;
}

export interface ProvisionResponse {
  gateway: number;
  site: number | null;
  devices: ProvisionedDevice[];
  // 一设备一任务:每台设备各派生一个任务。多设备时 task(单数,旧兼容字段)为 null,
  // 完整列表在 tasks 里。
  tasks: Array<{ id: number; code: string; device_id: number }>;
  task: { id: number; code: string } | null;
  created: { devices: number; points: number };
}

/** 网关默认值:与 backend/acquisition/protocols/scada.py 的 FieldSpec 默认值对齐。 */
export const DEFAULT_TOPIC_TEMPLATE =
  '/sys/{product_key}/device/{device_name}/thing/property/{code}/post';

export const GATEWAY_DEFAULTS: Partial<ScadaGatewayInput> = {
  source_port: 8883,
  mqtt_use_tls: true,
  mqtt_qos: 0,
  mqtt_read_timeout: 5,
  topic_template: DEFAULT_TOPIC_TEMPLATE,
};

const BASE = '/config/scada-gateways/';

/**
 * 获取全部网关。
 *
 * 标准 list 端点会被 DRF 全局分页包成 `{ results }`;`fetchAllPages` 同时
 * 兼容裸数组与分页信封,并逐页取全量(XIU-9 / H10)。
 */
export async function listGateways(): Promise<ScadaGateway[]> {
  return fetchAllPages<ScadaGateway>(async (limit, offset) => {
    const response = await apiClient.get(BASE, { params: { limit, offset } });
    return response.data;
  });
}

export async function createGateway(payload: ScadaGatewayInput): Promise<ScadaGateway> {
  const response = await apiClient.post<ScadaGateway>(BASE, payload);
  return response.data;
}

export async function updateGateway(
  id: number,
  payload: Partial<ScadaGatewayInput>,
): Promise<ScadaGateway> {
  const response = await apiClient.patch<ScadaGateway>(`${BASE}${id}/`, payload);
  return response.data;
}

export async function deleteGateway(id: number): Promise<void> {
  await apiClient.delete(`${BASE}${id}/`);
}

/**
 * 批量下发设备 + 测点(+ 可选采集任务)。
 *
 * 幂等:同一份 payload 重复提交是更新,不会重复建设备/测点。
 *
 * 这里带 `silent`,让 apiClient 的全局拦截器不要把校验错误 JSON 直接弹成
 * message —— 调用方用 `extractProvisionErrors` 逐条渲染更有用。
 */
export async function provision(
  gatewayId: number,
  payload: ProvisionPayload,
): Promise<ProvisionResponse> {
  const response = await apiClient.post<ProvisionResponse>(
    `${BASE}${gatewayId}/provision/`,
    payload,
    { silent: true } as AxiosRequestConfig & { silent: boolean },
  );
  return response.data;
}

/**
 * 下载两表 Excel 模板(「网关服务」一行 + 「设备与测点」每行一个测点)。
 *
 * 注意别和设备管理页那个通用模板搞混:通用模板是 40 列的单表大宽表,scada
 * 设备要在每一行重复填 broker/账号/密码/product_key —— 正是网关模型要消掉的
 * 重复。配 scada 一律用这份。
 */
export async function downloadScadaTemplate(): Promise<void> {
  await downloadFile(`${BASE}template/`, 'scada_template.xlsx');
}

/** 导出某个网关的完整配置(格式与模板一致,改完可直接再导入)。 */
export async function exportGateway(gatewayId: number, code: string): Promise<void> {
  const safe = code.replace(/[/\s]/g, '_');
  await downloadFile(`${BASE}${gatewayId}/export/`, `scada_${safe}.xlsx`);
}

/** 导入 Excel 时可选的「顺带建采集任务」参数。 */
export interface ScadaImportOptions {
  task_code?: string;
  task_name?: string;
  sample_rate_hz?: number;
}

/**
 * 导入两表 Excel。按 code 幂等(重复导入是更新),校验失败返回 400 且
 * **不写入任何数据**,所以失败时界面上什么都不用回滚。
 *
 * 同样带 `silent`:逐行错误由调用方渲染成列表,比全局 message 有用得多。
 */
export async function importScadaExcel(
  file: File,
  options: ScadaImportOptions = {},
): Promise<ProvisionResponse> {
  const body = new FormData();
  body.append('file', file);
  if (options.task_code) {
    body.append('task_code', options.task_code);
    if (options.task_name) body.append('task_name', options.task_name);
    if (options.sample_rate_hz !== undefined) {
      body.append('sample_rate_hz', String(options.sample_rate_hz));
    }
  }
  const response = await apiClient.post<ProvisionResponse>(`${BASE}import/`, body, {
    silent: true,
    // apiClient 实例默认 Content-Type: application/json,直接发 FormData 会被
    // DRF 的 MultiPartParser 拒成 415。这里把默认头去掉,让 axios/浏览器按
    // FormData 自己生成 multipart/form-data 并带上 boundary。
    headers: { 'Content-Type': undefined },
  } as AxiosRequestConfig & { silent: boolean });
  return response.data;
}

/**
 * 导入接口的错误是逐行的 `{row, column, message}`,和 provision 的嵌套
 * serializer 错误不是一个形状,单独压平成「第 N 行 · 列名:说明」。
 */
export function extractImportErrors(error: unknown): string[] {
  const data = (error as { response?: { data?: { errors?: unknown } } })?.response?.data;
  const rows = data?.errors;
  if (Array.isArray(rows) && rows.length > 0) {
    return rows.map((e) => {
      const { row, column, message } = e as { row?: number; column?: string; message?: string };
      const where = [row ? `第 ${row} 行` : '', column || ''].filter(Boolean).join(' · ');
      return where ? `${where}:${message ?? '格式有误'}` : message ?? '格式有误';
    });
  }
  return extractProvisionErrors(error);
}

/**
 * 把 DRF 的错误响应压平成可展示的字符串列表。
 *
 * DRF 校验错误可能是 `{field: ["msg"]}`、`{detail: "msg"}`,provision 这种
 * 嵌套 serializer 还会回 `{devices: [{points: [{code: ["msg"]}]}]}`,
 * 所以这里递归压平并保留字段路径。
 */
export function extractProvisionErrors(error: unknown): string[] {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  if (data === undefined || data === null) {
    const message = (error as { message?: string })?.message;
    return [message || '请求失败'];
  }
  if (typeof data === 'string') return [data];

  const out: string[] = [];
  const walk = (node: unknown, path: string) => {
    if (node === null || node === undefined) return;
    if (typeof node === 'string' || typeof node === 'number' || typeof node === 'boolean') {
      out.push(path ? `${path}: ${node}` : String(node));
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((item, index) => {
        // 数组元素是对象时(devices[0] 之类)带上下标,纯消息列表则不带。
        const isLeaf = typeof item !== 'object' || item === null;
        walk(item, isLeaf ? path : `${path}[${index + 1}]`);
      });
      return;
    }
    if (typeof node === 'object') {
      for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
        // `detail` / `non_field_errors` 没有有意义的字段名,不拼进路径。
        const isGeneric = key === 'detail' || key === 'non_field_errors';
        walk(value, isGeneric ? path : path ? `${path}.${key}` : key);
      }
    }
  };
  walk(data, '');
  return out.length > 0 ? out : ['请求失败'];
}
