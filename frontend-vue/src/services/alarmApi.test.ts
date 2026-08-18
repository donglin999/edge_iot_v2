import { apiClient } from './apiClient';
import {
  acknowledgeAlarm,
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
});
