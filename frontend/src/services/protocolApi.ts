/**
 * Protocol registry API.
 *
 * The backend exposes per-protocol field schemas at /api/acquisition/protocols/.
 * The frontend uses these to render the device-add and device-edit forms
 * dynamically — no per-protocol UI code needed.
 */
import type { AxiosRequestConfig } from 'axios';

import { apiClient, downloadFile } from './apiClient';

export type FieldKind = 'string' | 'int' | 'float' | 'bool' | 'enum' | 'secret';

export interface FieldSpec {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  default: unknown;
  choices: Array<string | number> | null;
  help_text: string;
  example: unknown;
}

export interface ProtocolDescriptor {
  name: string;
  label: string;
  category: 'fieldbus' | 'industrial-ethernet' | 'iot' | 'opc' | 'other';
  description: string;
  supports_pause: boolean;
  identity_fields: string[];
  device_fields: FieldSpec[];
  point_fields: FieldSpec[];
}

export async function listProtocols(): Promise<ProtocolDescriptor[]> {
  const response = await apiClient.get<ProtocolDescriptor[]>('/acquisition/protocols/');
  return response.data;
}

export async function getProtocol(name: string): Promise<ProtocolDescriptor> {
  const response = await apiClient.get<ProtocolDescriptor>(`/acquisition/protocols/${name}/`);
  return response.data;
}

export async function downloadTemplate(protocols?: string[]): Promise<void> {
  await downloadFile(
    '/acquisition/protocols/template/',
    'edge_iot_excel_template.xlsx',
    protocols && protocols.length > 0 ? { protocols: protocols.join(',') } : undefined,
  );
}

/**
 * Export the current devices/points to an .xlsx with the same column layout
 * as the import template — edit it and re-upload via the import flow.
 *
 * SCADA devices are always excluded (their connection config lives on the
 * gateway, not device.metadata) — use the gateway's own export for those.
 */
export async function downloadDeviceExport(protocols?: string[]): Promise<void> {
  await downloadFile(
    '/config/devices/export/',
    'edge_iot_devices_export.xlsx',
    protocols && protocols.length > 0 ? { protocols: protocols.join(',') } : undefined,
  );
}

/**
 * v2 每协议两表 Excel(docs/excel-import-export-v2.md)。
 *
 * 与上面的 40 列通用单表(legacy)是两套并存的端点:v2 每个工作簿只装一个协议,
 * 「设备」sheet 配一次连接参数,「测点」sheet 每行一个测点极简填 —— 解决 legacy
 * 大宽表「一个协议只用 7~8 列,连接参数在每行测点上重复」的问题。scada 已经有
 * 自己的两表网关流程(services/scadaApi.ts),不走这里;simulator 不是生产协议,
 * 同样排除,由调用方(DeviceListPage 的模板下拉生成逻辑)过滤。
 */
const PROTOCOL_EXCEL_BASE = '/config/protocol-excel/';

/** 导入/导出响应里 devices/points/tasks 三类计数。 */
export interface ProtocolImportCounts {
  devices: number;
  points: number;
  tasks: number;
}

/** 导入成功后,每台设备落库结果的摘要(供导入弹窗展示)。 */
export interface ProtocolImportDeviceResult {
  code: string;
  device_name: string;
  points: Array<{ id: number; code: string }>;
  /** 一设备一任务(既定约束),task-{设备编码} 幂等 upsert;后端实现里恒非空。 */
  task: string | null;
}

export interface ProtocolImportResponse {
  protocol: string;
  created: ProtocolImportCounts;
  updated: ProtocolImportCounts;
  devices: ProtocolImportDeviceResult[];
  errors: unknown[];
}

/** 行级错误:{sheet,row,column,message} —— 比 legacy 的 {row,column,message} 多一个 sheet 维度。 */
export interface ProtocolImportRowError {
  sheet?: string;
  row?: number;
  column?: string;
  message?: string;
}

/** 下载某协议的 v2 模板(设备 + 测点两表)。scada/simulator 不要传进这里。 */
export async function downloadProtocolTemplateV2(protocol: string): Promise<void> {
  await downloadFile(`${PROTOCOL_EXCEL_BASE}template/`, `${protocol}_template.xlsx`, { protocol });
}

/**
 * 导出某协议当前设备/测点配置,布局与模板一致——改完可直接再走
 * `importProtocolWorkbook` 导回(圆环:不改一字导回 = 0 created)。
 */
export async function exportProtocolDevices(protocol: string): Promise<void> {
  await downloadFile(`${PROTOCOL_EXCEL_BASE}export/`, `${protocol}_devices.xlsx`, { protocol });
}

/**
 * 单设备导出:只导这一台(v2 两表,可改完直接导回)。协议由后端从设备推断;
 * scada 设备后端会 400(应走网关页),调用方自行拦或让错误浮出。
 */
export async function exportSingleDevice(deviceId: number, deviceCode: string): Promise<void> {
  const safe = deviceCode.replace(/[/\s]/g, '_');
  await downloadFile(`${PROTOCOL_EXCEL_BASE}export/`, `${safe}.xlsx`, {
    device_ids: String(deviceId),
  });
}

/**
 * v2 同步导入(不走 ImportJob/celery):上传一份工作簿,后端自动识别协议
 * (优先读「使用说明」sheet 的元数据,退化到列签名匹配),整体一个事务 ——
 * 任何行级错误都 400 且不写任何数据。
 *
 * 带 `silent`:调用方用 `extractProtocolImportErrors` 逐行渲染,比全局
 * message 弹窗有用得多(与 scadaApi.importScadaExcel 同款约定)。
 */
export async function importProtocolWorkbook(file: File): Promise<ProtocolImportResponse> {
  const body = new FormData();
  body.append('file', file);
  const response = await apiClient.post<ProtocolImportResponse>(
    `${PROTOCOL_EXCEL_BASE}import/`,
    body,
    {
      silent: true,
      // apiClient 默认 Content-Type: application/json,发 FormData 要去掉这个
      // 默认头,让浏览器自己按 multipart/form-data 生成并带上 boundary。
      headers: { 'Content-Type': undefined },
    } as AxiosRequestConfig & { silent: boolean },
  );
  return response.data;
}

/**
 * 把 v2 导入的错误响应压平成可展示的字符串列表。
 *
 * 行级错误 {sheet,row,column,message} → 「sheet · 第 N 行 · 列:说明」
 * (与 scadaApi.extractImportErrors 同一压平契约,多一个 sheet 维度)。
 * 没有行级 errors 数组时(比如整份文件都认不出协议),退化到通用 DRF 错误
 * 压平,和 provision/legacy 导入的失败展示保持一致的观感。
 */
export function extractProtocolImportErrors(error: unknown): string[] {
  const data = (error as { response?: { data?: { errors?: unknown } } })?.response?.data;
  const rows = data?.errors;
  if (Array.isArray(rows) && rows.length > 0) {
    return rows.map((e) => {
      const { sheet, row, column, message } = e as ProtocolImportRowError;
      const where = [sheet || '', row ? `第 ${row} 行` : '', column || '']
        .filter(Boolean)
        .join(' · ');
      return where ? `${where}:${message ?? '格式有误'}` : message ?? '格式有误';
    });
  }
  return flattenApiError(error);
}

/**
 * 通用 DRF 错误压平(与 scadaApi.extractProvisionErrors 逻辑一致但独立一份 ——
 * 两个 service 模块不互相依赖,各自地盘各自兜底)。
 */
function flattenApiError(error: unknown): string[] {
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
        const isLeaf = typeof item !== 'object' || item === null;
        walk(item, isLeaf ? path : `${path}[${index + 1}]`);
      });
      return;
    }
    if (typeof node === 'object') {
      for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
        const isGeneric = key === 'detail' || key === 'non_field_errors';
        walk(value, isGeneric ? path : path ? `${path}.${key}` : key);
      }
    }
  };
  walk(data, '');
  return out.length > 0 ? out : ['请求失败'];
}

/**
 * 是否是「这份文件是旧版 40 列大宽表,不是 v2 两表格式」的错误 —— 命中时界面
 * 应提示改去「导入作业」页,而不是把一堆「缺 sheet」之类的行级错误甩给用户。
 *
 * 首选机器可读信号:后端错误行带 `kind: "format_unrecognized"`(显式契约,
 * 换文案不影响)。关键词匹配仅作旧响应/异常路径的兜底。
 */
const LEGACY_FORMAT_HINTS = [
  /legacy/i,
  /旧版/,
  /40\s*列/,
  /大宽表/,
  /无法识别协议/,
  /无法识别.*格式/,
  /识别不出/,
];

export function isLegacyFormatError(error: unknown): boolean {
  const data = (error as { response?: { data?: { errors?: unknown } } })?.response?.data;
  const rows = data?.errors;
  if (Array.isArray(rows) && rows.some(
    (e) => (e as { kind?: string })?.kind === 'format_unrecognized',
  )) {
    return true;
  }
  const messages = extractProtocolImportErrors(error);
  return messages.some((m) => LEGACY_FORMAT_HINTS.some((re) => re.test(m)));
}

/**
 * AntD Tag color for a protocol's category. Shared by every place that
 * renders a protocol badge (device list, device detail, ...) so they can't
 * drift into showing different colors for the same protocol.
 */
export function protocolTagColor(category?: ProtocolDescriptor['category']): string {
  switch (category) {
    case 'industrial-ethernet':
      return 'blue';
    case 'fieldbus':
      return 'orange';
    case 'iot':
      return 'purple';
    case 'opc':
      return 'cyan';
    default:
      return 'default';
  }
}
