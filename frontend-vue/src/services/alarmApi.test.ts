import { apiClient } from './apiClient';
import {
  acknowledgeAlarm,
  alarmRuleRangeError,
  createAlarmRule,
  deleteAlarmRule,
  fetchAlarmRules,
  fetchAlarms,
  updateAlarmRule,
  type AlarmRuleWritePayload,
} from './alarmApi';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('alarm API', () => {
  it('walks every filtered alarm page with the exact query contract', async () => {
    const controller = new AbortController();
    const getMock = vi
      .spyOn(apiClient, 'get')
      .mockResolvedValueOnce({
        data: {
          count: 2,
          next: '/api/acquisition/alarms/?limit=1000&offset=1&status=firing',
          previous: null,
          results: [{ id: 1, status: 'firing' }],
        },
      } as never)
      .mockResolvedValueOnce({
        data: {
          count: 2,
          next: null,
          previous: '/api/acquisition/alarms/?limit=1000&offset=0&status=firing',
          results: [{ id: 2, status: 'firing' }],
        },
      } as never);

    await expect(fetchAlarms('firing', controller.signal)).resolves.toHaveLength(2);
    expect(getMock).toHaveBeenNthCalledWith(1, '/acquisition/alarms/', {
      params: { limit: 1000, offset: 0, status: 'firing' },
      signal: controller.signal,
    });
    expect(getMock).toHaveBeenNthCalledWith(2, '/acquisition/alarms/', {
      params: { limit: 1000, offset: 1, status: 'firing' },
      signal: controller.signal,
    });
  });

  it('omits the status parameter for all alarms and paginates rules', async () => {
    const getMock = vi
      .spyOn(apiClient, 'get')
      .mockResolvedValueOnce({ data: [] } as never)
      .mockResolvedValueOnce({
        data: {
          count: 1,
          next: null,
          previous: null,
          results: [{ id: 9, name: '温度高' }],
        },
      } as never);

    await fetchAlarms();
    await fetchAlarmRules();

    expect(getMock).toHaveBeenNthCalledWith(1, '/acquisition/alarms/', {
      params: { limit: 1000, offset: 0 },
      signal: undefined,
    });
    expect(getMock).toHaveBeenNthCalledWith(2, '/acquisition/alarm-rules/', {
      params: { limit: 1000, offset: 0 },
      signal: undefined,
    });
  });

  it('keeps acknowledge and rule CRUD methods, URLs and payloads exact', async () => {
    const controller = new AbortController();
    const payload: AlarmRuleWritePayload = {
      name: '温度高',
      point_code: 'temperature',
      device_code: 'plc-1',
      operator: 'gt',
      threshold: 80,
      threshold_high: null,
      severity: 'critical',
      is_active: true,
      description: '温度超过安全值',
    };
    const postMock = vi
      .spyOn(apiClient, 'post')
      .mockResolvedValueOnce({ data: { id: 7, status: 'acked' } } as never)
      .mockResolvedValueOnce({ data: { id: 9, ...payload } } as never);
    const patchMock = vi
      .spyOn(apiClient, 'patch')
      .mockResolvedValue({ data: { id: 9, ...payload, is_active: false } } as never);
    const deleteMock = vi
      .spyOn(apiClient, 'delete')
      .mockResolvedValue({ data: undefined } as never);

    await acknowledgeAlarm(7, controller.signal);
    await createAlarmRule(payload, controller.signal);
    await updateAlarmRule(9, { ...payload, is_active: false }, controller.signal);
    await deleteAlarmRule(9, controller.signal);

    expect(postMock).toHaveBeenNthCalledWith(
      1,
      '/acquisition/alarms/7/ack/',
      undefined,
      { signal: controller.signal },
    );
    expect(postMock).toHaveBeenNthCalledWith(
      2,
      '/acquisition/alarm-rules/',
      payload,
      { signal: controller.signal },
    );
    expect(patchMock).toHaveBeenCalledWith(
      '/acquisition/alarm-rules/9/',
      { ...payload, is_active: false },
      { signal: controller.signal },
    );
    expect(deleteMock).toHaveBeenCalledWith('/acquisition/alarm-rules/9/', {
      signal: controller.signal,
    });
  });

  it.each(['between', 'outside'] as const)(
    'requires threshold_high for %s rules before issuing HTTP',
    async (operator) => {
      const postMock = vi.spyOn(apiClient, 'post');
      const payload: AlarmRuleWritePayload = {
        name: '区间规则',
        point_code: 'temperature',
        device_code: '',
        operator,
        threshold: 10,
        threshold_high: null,
        severity: 'warning',
        is_active: true,
        description: '',
      };

      expect(alarmRuleRangeError(payload)).toBe('区间规则必须填写阈值上限');
      await expect(createAlarmRule(payload)).rejects.toThrow('区间规则必须填写阈值上限');
      expect(postMock).not.toHaveBeenCalled();
    },
  );

  it.each(['between', 'outside'] as const)(
    'rejects an inverted %s range before issuing HTTP',
    async (operator) => {
      const patchMock = vi.spyOn(apiClient, 'patch');
      const payload: AlarmRuleWritePayload = {
        name: '倒置区间',
        point_code: 'pressure',
        device_code: '',
        operator,
        threshold: 20,
        threshold_high: 10,
        severity: 'critical',
        is_active: true,
        description: '',
      };

      expect(alarmRuleRangeError(payload)).toBe('阈值上限不能小于阈值下限');
      await expect(updateAlarmRule(3, payload)).rejects.toThrow(
        '阈值上限不能小于阈值下限',
      );
      expect(patchMock).not.toHaveBeenCalled();
    },
  );

  it.each(['between', 'outside'] as const)(
    'preserves a valid %s payload exactly',
    async (operator) => {
      const payload: AlarmRuleWritePayload = {
        name: '有效区间',
        point_code: 'temperature',
        device_code: 'plc-1',
        operator,
        threshold: 10,
        threshold_high: 20,
        severity: 'warning',
        is_active: true,
        description: '范围测试',
      };
      const postMock = vi
        .spyOn(apiClient, 'post')
        .mockResolvedValue({ data: { id: 21, ...payload } } as never);

      await createAlarmRule(payload);

      expect(postMock).toHaveBeenCalledWith(
        '/acquisition/alarm-rules/',
        payload,
        { signal: undefined },
      );
    },
  );
});
