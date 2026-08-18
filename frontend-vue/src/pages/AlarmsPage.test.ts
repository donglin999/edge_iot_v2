import { fireEvent, render, screen, waitFor, within } from '@testing-library/vue';
import { App as AntApp } from 'ant-design-vue';
import { defineComponent, h } from 'vue';

import {
  acknowledgeAlarm,
  createAlarmRule,
  deleteAlarmRule,
  fetchAlarmRules,
  fetchAlarms,
  updateAlarmRule,
  type AlarmRecord,
  type AlarmRule,
} from '@/services/alarmApi';

import AlarmsPage from './AlarmsPage.vue';

vi.mock('@/services/alarmApi', () => ({
  acknowledgeAlarm: vi.fn(),
  createAlarmRule: vi.fn(),
  deleteAlarmRule: vi.fn(),
  fetchAlarmRules: vi.fn(),
  fetchAlarms: vi.fn(),
  updateAlarmRule: vi.fn(),
}));

const alarmRecords: AlarmRecord[] = [
  {
    id: 1,
    rule: 11,
    rule_name: '温度高',
    category: 'threshold',
    severity: 'critical',
    point_code: 'temperature',
    device_code: 'plc-1',
    value: 91.2,
    status: 'firing',
    fired_at: '2026-08-18T08:00:00Z',
    message: '温度超过阈值',
  },
  {
    id: 2,
    rule: null,
    rule_name: null,
    category: 'connectivity',
    severity: 'warning',
    point_code: '',
    device_code: 'plc-2',
    value: {},
    status: 'acked',
    fired_at: '2026-08-18T07:00:00Z',
    message: '设备连接中断',
  },
];

const alarmRules: AlarmRule[] = [
  {
    id: 11,
    name: '温度高',
    point_code: 'temperature',
    device_code: 'plc-1',
    operator: 'gt',
    threshold: 80,
    threshold_high: null,
    severity: 'critical',
    is_active: true,
    description: '温度超过安全值',
  },
];

const TestHost = defineComponent({
  name: 'AlarmsPageTestHost',
  setup: () => () => h(AntApp, null, { default: () => h(AlarmsPage) }),
});

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.mocked(fetchAlarms).mockResolvedValue(alarmRecords);
  vi.mocked(fetchAlarmRules).mockResolvedValue(alarmRules);
  vi.mocked(acknowledgeAlarm).mockResolvedValue({
    ...alarmRecords[0]!,
    status: 'acked',
  });
  vi.mocked(createAlarmRule).mockResolvedValue({ ...alarmRules[0]!, id: 12, name: '压力高' });
  vi.mocked(updateAlarmRule).mockResolvedValue(alarmRules[0]!);
  vi.mocked(deleteAlarmRule).mockResolvedValue();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('AlarmsPage', () => {
  it('exposes initial loading then renders alarms, categories and firing count', async () => {
    const pending = deferred<AlarmRecord[]>();
    vi.mocked(fetchAlarms).mockReturnValueOnce(pending.promise);

    render(TestHost);

    expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'true');
    expect(fetchAlarms).toHaveBeenCalledWith('all', expect.any(AbortSignal));
    expect(fetchAlarmRules).toHaveBeenCalledWith(expect.any(AbortSignal));

    pending.resolve(alarmRecords);

    expect(await screen.findByText('当前有 1 个未确认告警')).toBeInTheDocument();
    expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    expect(screen.getByText('温度高')).toBeInTheDocument();
    expect(screen.getByText('设备连接中断')).toBeInTheDocument();
    expect(screen.getByText('连接')).toBeInTheDocument();
    expect(screen.getByText('91.2')).toBeInTheDocument();
  });

  it('filters records and acknowledges a firing alarm with a refresh', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('radio', { name: '已确认' }));
    await waitFor(() => {
      expect(fetchAlarms).toHaveBeenLastCalledWith('acked', expect.any(AbortSignal));
    });

    await fireEvent.click(screen.getByRole('button', { name: '确认告警 1' }));
    await waitFor(() => {
      expect(acknowledgeAlarm).toHaveBeenCalledWith(1, expect.any(AbortSignal));
    });
    expect(fetchAlarms).toHaveBeenCalledTimes(3);
  });

  it('shows the load failure without leaving the page in a busy state', async () => {
    vi.mocked(fetchAlarms).mockRejectedValueOnce(new Error('network down'));

    render(TestHost);

    expect(
      await screen.findByText('告警数据加载失败：network down'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
  });

  it('renders explicit alarm and rule empty states', async () => {
    vi.mocked(fetchAlarms).mockResolvedValueOnce([]);
    vi.mocked(fetchAlarmRules).mockResolvedValueOnce([]);

    render(TestHost);

    expect(await screen.findByText('无告警')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (0)' }));
    expect(
      await screen.findByText('尚未创建任何规则，点击右上「新建规则」开始'),
    ).toBeInTheDocument();
  });

  it('creates a rule from the rules tab with the complete writable payload', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    await fireEvent.click(screen.getByRole('button', { name: '新建规则' }));

    const dialog = await screen.findByRole('dialog');
    await fireEvent.update(within(dialog).getByLabelText('规则名称'), '压力高');
    await fireEvent.update(within(dialog).getByLabelText('测点编码'), 'pressure');
    await fireEvent.update(within(dialog).getByLabelText('设备编码（留空匹配所有）'), 'plc-3');
    await fireEvent.update(within(dialog).getByLabelText('阈值'), '12.5');
    await fireEvent.update(within(dialog).getByLabelText('描述'), '压力超过安全值');
    await fireEvent.click(within(dialog).getByRole('button', { name: /保\s*存/ }));

    await waitFor(() => {
      expect(createAlarmRule).toHaveBeenCalledWith(
        {
          name: '压力高',
          point_code: 'pressure',
          device_code: 'plc-3',
          operator: 'gt',
          threshold: 12.5,
          threshold_high: null,
          severity: 'warning',
          is_active: true,
          description: '压力超过安全值',
        },
        expect.any(AbortSignal),
      );
    });
  });
});
