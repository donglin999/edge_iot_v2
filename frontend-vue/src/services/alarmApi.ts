import { apiClient } from './apiClient';
import { fetchAllPages } from './pagination';

export type AlarmSeverity = 'info' | 'warning' | 'critical';
export type AlarmCategory = 'threshold' | 'connectivity' | 'system' | 'lifecycle';
export type AlarmStatus = 'firing' | 'acked' | 'cleared';
export type AlarmStatusFilter = 'all' | AlarmStatus;
export type AlarmOperator = 'gt' | 'ge' | 'lt' | 'le' | 'eq' | 'ne' | 'between' | 'outside';

const RANGE_OPERATORS: ReadonlySet<AlarmOperator> = new Set(['between', 'outside']);

export interface AlarmRecord {
  id: number;
  rule: number | null;
  rule_name: string | null;
  category?: AlarmCategory;
  dedup_key?: string;
  severity: AlarmSeverity;
  point_code: string;
  device_code: string;
  value: unknown;
  status: AlarmStatus;
  fired_at: string;
  message: string;
}

export interface AlarmRuleWritePayload {
  name: string;
  point_code: string;
  device_code: string;
  operator: AlarmOperator;
  threshold: number | null;
  threshold_high: number | null;
  severity: AlarmSeverity;
  is_active: boolean;
  description: string;
}

export interface AlarmRule extends AlarmRuleWritePayload {
  id: number;
  created_at?: string;
  updated_at?: string;
}

export function isRangeAlarmOperator(operator: AlarmOperator): boolean {
  return RANGE_OPERATORS.has(operator);
}

export function alarmRuleRangeError(
  payload: Pick<AlarmRuleWritePayload, 'operator' | 'threshold' | 'threshold_high'>,
): string | null {
  if (!isRangeAlarmOperator(payload.operator)) return null;
  if (payload.threshold == null) return '区间规则必须填写阈值下限';
  if (payload.threshold_high == null) return '区间规则必须填写阈值上限';
  if (payload.threshold_high < payload.threshold) return '阈值上限不能小于阈值下限';
  return null;
}

function assertValidAlarmRuleRange(payload: AlarmRuleWritePayload): void {
  const validationError = alarmRuleRangeError(payload);
  if (validationError) throw new Error(validationError);
}

export async function fetchAlarms(
  status: AlarmStatusFilter = 'all',
  signal?: AbortSignal,
): Promise<AlarmRecord[]> {
  return fetchAllPages<AlarmRecord>(async (limit, offset) => {
    const response = await apiClient.get('/acquisition/alarms/', {
      params: {
        limit,
        offset,
        ...(status === 'all' ? {} : { status }),
      },
      signal,
    });
    return response.data;
  });
}

export async function fetchAlarmRules(signal?: AbortSignal): Promise<AlarmRule[]> {
  return fetchAllPages<AlarmRule>(async (limit, offset) => {
    const response = await apiClient.get('/acquisition/alarm-rules/', {
      params: { limit, offset },
      signal,
    });
    return response.data;
  });
}

export async function acknowledgeAlarm(
  id: number,
  signal?: AbortSignal,
): Promise<AlarmRecord> {
  const response = await apiClient.post<AlarmRecord>(
    `/acquisition/alarms/${id}/ack/`,
    undefined,
    { signal },
  );
  return response.data;
}

export async function createAlarmRule(
  payload: AlarmRuleWritePayload,
  signal?: AbortSignal,
): Promise<AlarmRule> {
  assertValidAlarmRuleRange(payload);
  const response = await apiClient.post<AlarmRule>(
    '/acquisition/alarm-rules/',
    payload,
    { signal },
  );
  return response.data;
}

export async function updateAlarmRule(
  id: number,
  payload: AlarmRuleWritePayload,
  signal?: AbortSignal,
): Promise<AlarmRule> {
  assertValidAlarmRuleRange(payload);
  const response = await apiClient.patch<AlarmRule>(
    `/acquisition/alarm-rules/${id}/`,
    payload,
    { signal },
  );
  return response.data;
}

export async function deleteAlarmRule(
  id: number,
  signal?: AbortSignal,
): Promise<void> {
  await apiClient.delete(`/acquisition/alarm-rules/${id}/`, { signal });
}
