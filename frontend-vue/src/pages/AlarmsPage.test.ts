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

vi.mock('@/services/alarmApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/alarmApi')>();
  return {
    ...actual,
    acknowledgeAlarm: vi.fn(),
    createAlarmRule: vi.fn(),
    deleteAlarmRule: vi.fn(),
    fetchAlarmRules: vi.fn(),
    fetchAlarms: vi.fn(),
    updateAlarmRule: vi.fn(),
  };
});

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

const inactiveRangeDraft: AlarmRule = {
  id: 22,
  name: '温度区间草稿',
  point_code: 'temperature',
  device_code: '',
  operator: 'between',
  threshold: null,
  threshold_high: null,
  severity: 'warning',
  is_active: false,
  description: '阈值待补充',
};

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
  vi.mocked(fetchAlarms).mockReset().mockResolvedValue(alarmRecords);
  vi.mocked(fetchAlarmRules).mockReset().mockResolvedValue(alarmRules);
  vi.mocked(acknowledgeAlarm).mockReset().mockResolvedValue({
    ...alarmRecords[0]!,
    status: 'acked',
  });
  vi.mocked(createAlarmRule)
    .mockReset()
    .mockResolvedValue({ ...alarmRules[0]!, id: 12, name: '压力高' });
  vi.mocked(updateAlarmRule).mockReset().mockResolvedValue(alarmRules[0]!);
  vi.mocked(deleteAlarmRule).mockReset().mockResolvedValue();
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
    await waitFor(() => {
      expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    });
    expect(screen.getByText('温度高')).toBeInTheDocument();
    expect(await screen.findByText('设备连接中断')).toBeInTheDocument();
    expect(screen.getByText('连接')).toBeInTheDocument();
    expect(screen.getByText('91.2')).toBeInTheDocument();
  });

  it('renders alarm results without waiting for the rule request', async () => {
    const pendingRules = deferred<AlarmRule[]>();
    vi.mocked(fetchAlarmRules).mockReturnValueOnce(pendingRules.promise);

    render(TestHost);

    expect(await screen.findByText('当前有 1 个未确认告警')).toBeInTheDocument();
    expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('tab', { name: '阈值规则 (0)' })).toBeInTheDocument();

    pendingRules.resolve(alarmRules);
    await waitFor(() => {
      expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    });
    expect(screen.getByRole('tab', { name: '阈值规则 (1)' })).toBeInTheDocument();
  });

  it('renders rule results without waiting for the alarm request', async () => {
    const pendingAlarms = deferred<AlarmRecord[]>();
    vi.mocked(fetchAlarms).mockReturnValueOnce(pendingAlarms.promise);

    render(TestHost);

    expect(await screen.findByRole('tab', { name: '阈值规则 (1)' })).toBeInTheDocument();
    expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('tab', { name: '告警记录 (0)' })).toBeInTheDocument();

    pendingAlarms.resolve(alarmRecords);
    expect(await screen.findByText('当前有 1 个未确认告警')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    });
  });

  it('replaces rows with the real acked-only response when filtering', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    vi.mocked(fetchAlarms).mockResolvedValueOnce([alarmRecords[1]!]);
    await fireEvent.click(screen.getByRole('radio', { name: '已确认' }));
    await waitFor(() => {
      expect(fetchAlarms).toHaveBeenLastCalledWith('acked', expect.any(AbortSignal));
    });
    await waitFor(() => {
      expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    });

    expect(screen.getByText('设备连接中断')).toBeInTheDocument();
    expect(screen.queryByText('温度高')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认告警 1' })).not.toBeInTheDocument();
    expect(screen.queryByText('当前有 1 个未确认告警')).not.toBeInTheDocument();
  });

  it('acknowledges from the firing view and refreshes to the acked state', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    vi.mocked(fetchAlarms).mockResolvedValueOnce(
      alarmRecords.map((alarm) => ({ ...alarm, status: 'acked' })),
    );
    await fireEvent.click(screen.getByRole('button', { name: '确认告警 1' }));
    await waitFor(() => {
      expect(acknowledgeAlarm).toHaveBeenCalledWith(1, expect.any(AbortSignal));
    });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '确认告警 1' })).not.toBeInTheDocument();
    });
    expect(fetchAlarms).toHaveBeenCalledTimes(2);
  });

  it('keeps rules available when alarm loading fails', async () => {
    vi.mocked(fetchAlarms).mockRejectedValueOnce(new Error('network down'));

    render(TestHost);

    expect(
      await screen.findByText('告警记录加载失败：network down'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    expect(screen.getByRole('tab', { name: '阈值规则 (1)' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '告警记录 (0)' })).toBeInTheDocument();
  });

  it('keeps alarms available when rule loading fails', async () => {
    vi.mocked(fetchAlarmRules).mockRejectedValueOnce(new Error('rules offline'));

    render(TestHost);

    expect(
      await screen.findByText('告警规则加载失败：rules offline'),
    ).toBeInTheDocument();
    expect(screen.getByText('当前有 1 个未确认告警')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '告警记录 (2)' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '阈值规则 (0)' })).toBeInTheDocument();
  });

  it('clears old rows when a new status filter fails', async () => {
    render(TestHost);
    await screen.findByText('温度高');

    vi.mocked(fetchAlarms).mockRejectedValueOnce(new Error('acked unavailable'));
    await fireEvent.click(screen.getByRole('radio', { name: '已确认' }));

    expect(
      await screen.findByText('告警记录加载失败：acked unavailable'),
    ).toBeInTheDocument();
    expect(screen.queryByText('温度高')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认告警 1' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '告警记录 (0)' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '阈值规则 (1)' })).toBeInTheDocument();
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

  it('locks the rule editor and deduplicates saves until the request settles', async () => {
    const pendingCreate = deferred<AlarmRule>();
    vi.mocked(createAlarmRule).mockReturnValueOnce(pendingCreate.promise);

    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');
    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    await fireEvent.click(screen.getByRole('button', { name: '新建规则' }));

    const dialog = await screen.findByRole('dialog');
    await fireEvent.update(within(dialog).getByLabelText('规则名称'), '压力高');
    await fireEvent.update(within(dialog).getByLabelText('测点编码'), 'pressure');
    await fireEvent.update(within(dialog).getByLabelText('阈值'), '12.5');
    const saveButton = within(dialog).getByRole('button', { name: /保\s*存/ });
    await fireEvent.click(saveButton);
    await fireEvent.click(saveButton);

    await waitFor(() => expect(createAlarmRule).toHaveBeenCalledTimes(1));
    const cancelButton = within(dialog).getByRole('button', { name: /Cancel|取\s*消/ });
    expect(cancelButton).toBeDisabled();
    expect(screen.getByRole('button', { name: '新建规则' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '编辑规则 温度高' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '删除规则 温度高' })).toBeDisabled();
    await fireEvent.click(cancelButton);
    expect(screen.getByRole('dialog')).toBeVisible();

    const modalElement = dialog.querySelector('.ant-modal');
    expect(modalElement).not.toBeNull();
    pendingCreate.resolve({ ...alarmRules[0]!, id: 12, name: '压力高' });
    await waitFor(() => expect(modalElement).not.toBeVisible());
    expect(createAlarmRule).toHaveBeenCalledTimes(1);
  });

  it('prefills an edited rule and updates the exact id and writable payload', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    await fireEvent.click(screen.getByRole('button', { name: '编辑规则 温度高' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('编辑规则')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('规则名称')).toHaveValue('温度高');
    expect(within(dialog).getByLabelText('测点编码')).toHaveValue('temperature');
    expect(within(dialog).getByLabelText('设备编码（留空匹配所有）')).toHaveValue('plc-1');
    expect(within(dialog).getByLabelText('阈值')).toHaveValue('80');
    expect(within(dialog).getByText('> 大于')).toBeInTheDocument();
    expect(within(dialog).getByText('🔴 critical 严重')).toBeInTheDocument();
    expect(within(dialog).getByRole('switch', { name: '启用规则' })).toBeChecked();
    expect(within(dialog).getByLabelText('描述')).toHaveValue('温度超过安全值');

    await fireEvent.update(within(dialog).getByLabelText('规则名称'), '温度超高');
    await fireEvent.update(within(dialog).getByLabelText('描述'), '更新后的安全值');
    await fireEvent.click(within(dialog).getByRole('button', { name: /保\s*存/ }));

    await waitFor(() => {
      expect(updateAlarmRule).toHaveBeenCalledWith(
        11,
        {
          name: '温度超高',
          point_code: 'temperature',
          device_code: 'plc-1',
          operator: 'gt',
          threshold: 80,
          threshold_high: null,
          severity: 'critical',
          is_active: true,
          description: '更新后的安全值',
        },
        expect.any(AbortSignal),
      );
    });
    const updateSignal = vi.mocked(updateAlarmRule).mock.calls[0]![2];
    expect(updateSignal).toBeInstanceOf(AbortSignal);
    expect(updateSignal?.aborted).toBe(false);
  });

  it('creates an inactive range draft without requiring either threshold', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    await fireEvent.click(screen.getByRole('button', { name: '新建规则' }));

    const dialog = await screen.findByRole('dialog');
    await fireEvent.update(within(dialog).getByLabelText('规则名称'), '停用区间草稿');
    await fireEvent.update(within(dialog).getByLabelText('测点编码'), 'temperature');
    await fireEvent.mouseDown(within(dialog).getByLabelText('操作符'));
    await fireEvent.click(await screen.findByText('∈ 区间内'));
    await fireEvent.click(within(dialog).getByRole('switch', { name: '启用规则' }));
    await fireEvent.update(within(dialog).getByLabelText('描述'), '稍后补充阈值');
    await fireEvent.click(within(dialog).getByRole('button', { name: /保\s*存/ }));

    await waitFor(() => {
      expect(createAlarmRule).toHaveBeenCalledWith(
        {
          name: '停用区间草稿',
          point_code: 'temperature',
          device_code: '',
          operator: 'between',
          threshold: null,
          threshold_high: null,
          severity: 'warning',
          is_active: false,
          description: '稍后补充阈值',
        },
        expect.any(AbortSignal),
      );
    });
  });

  it('edits an existing inactive range draft without forcing thresholds', async () => {
    vi.mocked(fetchAlarmRules).mockResolvedValueOnce([inactiveRangeDraft]);
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    await fireEvent.click(
      screen.getByRole('button', { name: '编辑规则 温度区间草稿' }),
    );

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('switch', { name: '启用规则' })).not.toBeChecked();
    await fireEvent.update(within(dialog).getByLabelText('描述'), '继续保持停用');
    await fireEvent.click(within(dialog).getByRole('button', { name: /保\s*存/ }));

    await waitFor(() => {
      expect(updateAlarmRule).toHaveBeenCalledWith(
        22,
        {
          name: '温度区间草稿',
          point_code: 'temperature',
          device_code: '',
          operator: 'between',
          threshold: null,
          threshold_high: null,
          severity: 'warning',
          is_active: false,
          description: '继续保持停用',
        },
        expect.any(AbortSignal),
      );
    });
  });

  it('deletes a rule only after the confirmation dialog is accepted', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    const deleteButton = screen.getByRole('button', { name: '删除规则 温度高' });
    await fireEvent.click(deleteButton);

    const cancelDialog = await screen.findByRole('dialog');
    expect(within(cancelDialog).getByText('删除规则 "温度高" ?')).toBeInTheDocument();
    await fireEvent.click(
      within(cancelDialog).getByRole('button', { name: /Cancel|取\s*消/ }),
    );
    const cancelledModal = cancelDialog.querySelector('.ant-modal');
    expect(cancelledModal).not.toBeNull();
    await waitFor(() => {
      expect(cancelledModal).not.toBeVisible();
    });
    expect(deleteAlarmRule).not.toHaveBeenCalled();

    await fireEvent.click(deleteButton);
    await fireEvent.click(await screen.findByRole('button', { name: /OK|确\s*定/ }));

    await waitFor(() => {
      expect(deleteAlarmRule).toHaveBeenCalledWith(11, expect.any(AbortSignal));
    });
    expect(deleteAlarmRule).toHaveBeenCalledTimes(1);
    const deleteSignal = vi.mocked(deleteAlarmRule).mock.calls[0]![1];
    expect(deleteSignal).toBeInstanceOf(AbortSignal);
    expect(deleteSignal?.aborted).toBe(false);
  });

  it('allows only one delete confirmation and one pending delete request', async () => {
    const pendingDelete = deferred<void>();
    vi.mocked(deleteAlarmRule).mockReturnValueOnce(pendingDelete.promise);

    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');
    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    const deleteButton = screen.getByRole('button', { name: '删除规则 温度高' });
    await fireEvent.click(deleteButton);
    await fireEvent.click(deleteButton);

    expect(screen.getAllByText('删除规则 "温度高" ?')).toHaveLength(1);
    const confirmButton = await screen.findByRole('button', { name: /OK|确\s*定/ });
    await fireEvent.click(confirmButton);
    await fireEvent.click(confirmButton);

    await waitFor(() => expect(deleteAlarmRule).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: '编辑规则 温度高' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '删除规则 温度高' })).toBeDisabled();

    pendingDelete.resolve(undefined);
    await waitFor(() => expect(deleteButton).not.toBeDisabled());
    expect(deleteAlarmRule).toHaveBeenCalledTimes(1);
  });

  it('aborts active read and write signals when the page unmounts', async () => {
    vi.mocked(acknowledgeAlarm).mockImplementationOnce((_id, signal) =>
      new Promise<AlarmRecord>((_resolve, reject) => {
        signal?.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        );
      }),
    );
    const { unmount } = render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');
    await waitFor(() => {
      expect(screen.getByTestId('alarms-page')).toHaveAttribute('aria-busy', 'false');
    });

    const pendingAlarms = deferred<AlarmRecord[]>();
    const pendingRules = deferred<AlarmRule[]>();
    vi.mocked(fetchAlarms).mockReturnValueOnce(pendingAlarms.promise);
    vi.mocked(fetchAlarmRules).mockReturnValueOnce(pendingRules.promise);
    await fireEvent.click(screen.getByRole('button', { name: '刷新告警' }));
    await waitFor(() => expect(fetchAlarms).toHaveBeenCalledTimes(2));

    await fireEvent.click(screen.getByRole('button', { name: '确认告警 1' }));
    await waitFor(() => expect(acknowledgeAlarm).toHaveBeenCalledTimes(1));

    const alarmReadSignal = vi.mocked(fetchAlarms).mock.calls[1]![1];
    const ruleReadSignal = vi.mocked(fetchAlarmRules).mock.calls[1]![0];
    const writeSignal = vi.mocked(acknowledgeAlarm).mock.calls[0]![1];
    expect(alarmReadSignal?.aborted).toBe(false);
    expect(ruleReadSignal?.aborted).toBe(false);
    expect(writeSignal?.aborted).toBe(false);

    unmount();

    expect(alarmReadSignal?.aborted).toBe(true);
    expect(ruleReadSignal?.aborted).toBe(true);
    expect(writeSignal?.aborted).toBe(true);
    pendingAlarms.resolve(alarmRecords);
    pendingRules.resolve(alarmRules);
  });

  it('validates and submits a between rule as an ordered interval', async () => {
    render(TestHost);
    await screen.findByText('当前有 1 个未确认告警');

    await fireEvent.click(screen.getByRole('tab', { name: '阈值规则 (1)' }));
    await fireEvent.click(screen.getByRole('button', { name: '新建规则' }));

    const dialog = await screen.findByRole('dialog');
    await fireEvent.update(within(dialog).getByLabelText('规则名称'), '温度区间');
    await fireEvent.update(within(dialog).getByLabelText('测点编码'), 'temperature');
    await fireEvent.mouseDown(within(dialog).getByLabelText('操作符'));
    await fireEvent.click(await screen.findByText('∈ 区间内'));
    await fireEvent.update(within(dialog).getByLabelText('阈值'), '20');

    const saveButton = within(dialog).getByRole('button', { name: /保\s*存/ });
    await fireEvent.click(saveButton);
    expect(
      await within(dialog).findByText('区间规则必须填写阈值上限'),
    ).toBeInTheDocument();
    expect(createAlarmRule).not.toHaveBeenCalled();

    await fireEvent.update(within(dialog).getByLabelText('阈值上限（区间）'), '10');
    await fireEvent.click(saveButton);
    expect(
      await within(dialog).findByText('阈值上限不能小于阈值下限'),
    ).toBeInTheDocument();
    expect(createAlarmRule).not.toHaveBeenCalled();

    await fireEvent.update(within(dialog).getByLabelText('阈值上限（区间）'), '25');
    await fireEvent.click(saveButton);
    await waitFor(() => {
      expect(createAlarmRule).toHaveBeenCalledWith(
        expect.objectContaining({
          name: '温度区间',
          point_code: 'temperature',
          operator: 'between',
          threshold: 20,
          threshold_high: 25,
        }),
        expect.any(AbortSignal),
      );
    });
  });
});
