/**
 * 数据可视化页——历史图展示模式(team-prompts scada-topic-and-storage / Agent D):
 *   - 默认「全量数据」:PointChart 不带 window,导出 CSV 也不带 window(行为不变);
 *   - 切「自动降采样」:按当前时间范围查映射表(1h → 10s),PointChart 与
 *     导出 CSV 均带上 window,图上副标题注明"每 10s 取最新一条";
 *   - 自动降采样 + 近 5 分钟:映射为 null,仍走全量(不带 window)。
 *
 * PointChart 被 mock 成回显 window prop 的占位节点;导出路径直接断言
 * fetchPointHistory 的调用参数(带 / 不带 window)。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/acquisitionApi', () => ({
  fetchTasks: vi.fn(),
}));
vi.mock('../services/dataApi', () => ({
  fetchPointsLatestValues: vi.fn(),
  fetchPointHistory: vi.fn(),
}));
// 图表本体(recharts)与展示模式的传参无关,mock 成回显 window 的占位节点。
vi.mock('../components/PointChart', () => ({
  default: (props: { window?: string }) => (
    <div data-testid="point-chart" data-window={props.window ?? ''} />
  ),
}));

import { fetchTasks } from '../services/acquisitionApi';
import { fetchPointHistory, fetchPointsLatestValues } from '../services/dataApi';
import type { PointLatestValue } from '../services/dataApi';
import DataVisualizationPage from './DataVisualizationPage';

const TASKS = [{ id: 1, code: 'T1', name: '任务一', is_active: true }];

const POINTS: PointLatestValue[] = [
  {
    point_code: 'P1',
    point_name: '温度',
    unit: '℃',
    data_type: 'float',
    device_id: 1,
    device_name: '设备A',
    value: 23.4,
    quality: 'good',
    timestamp: '2026-07-28T12:00:00Z',
  },
];

function stubApi() {
  vi.mocked(fetchTasks).mockResolvedValue(TASKS as never);
  vi.mocked(fetchPointsLatestValues).mockResolvedValue({
    filter: { task_id: null, device_id: null, point_code: null },
    count: POINTS.length,
    points: POINTS,
  });
  vi.mocked(fetchPointHistory).mockResolvedValue({
    point_code: 'P1',
    start_time: null,
    end_time: null,
    count: 0,
    data: [],
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DataVisualizationPage />
    </MemoryRouter>,
  );
}

/** 选中 P1 测点,等历史面板(mock 的图表节点)出现。 */
async function selectPoint(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText('P1'));
  await screen.findByTestId('point-chart');
}

async function pickDisplayMode(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
) {
  // 展示模式选择器当前显示「全量数据」;副标题里也有同文案,用
  // selection-item 的 selector 精确定位选择器本体再点开选目标项。
  await user.click(
    screen.getByText('全量数据', { selector: '.ant-select-selection-item' }),
  );
  await user.click(await screen.findByTitle(label));
}

const chartWindow = () =>
  screen.getByTestId('point-chart').getAttribute('data-window');

/** 最近一次导出调用的 window 实参(fetchPointHistory 第 6 个参数)。 */
const lastExportWindow = () => {
  const calls = vi.mocked(fetchPointHistory).mock.calls;
  return calls[calls.length - 1][5];
};

describe('历史图展示模式(全量 / 自动降采样)', () => {
  beforeEach(() => {
    stubApi();
    // jsdom 没有 createObjectURL;导出走 <a download> 点击,拦掉真实点击。
    URL.createObjectURL = vi.fn(() => 'blob:mock');
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
      /* 拦掉 <a download> 的真实点击,jsdom 不支持导航 */
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it('默认「全量数据」:图表不带 window,导出也不带 window', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectPoint(user);

    expect(chartWindow()).toBe('');
    expect(screen.getByText('全量数据', { selector: 'span.ant-typography' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /导出 CSV/ }));
    await waitFor(() => expect(fetchPointHistory).toHaveBeenCalled());
    expect(lastExportWindow()).toBeUndefined();
  });

  it('切「自动降采样」(默认近 1 小时):图表与导出带 window=10s,副标题注明', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectPoint(user);

    await pickDisplayMode(user, '自动降采样');
    await waitFor(() => expect(chartWindow()).toBe('10s'));
    expect(screen.getByText('已降采样:每 10s 取最新一条')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /导出 CSV/ }));
    await waitFor(() => expect(fetchPointHistory).toHaveBeenCalled());
    expect(lastExportWindow()).toBe('10s');
  });

  it('自动降采样切时间范围跟着换窗口:6 小时 → 1m,5 分钟 → 全量', async () => {
    const user = userEvent.setup();
    renderPage();
    await selectPoint(user);

    await pickDisplayMode(user, '自动降采样');
    await waitFor(() => expect(chartWindow()).toBe('10s'));

    await user.click(screen.getByText('近 6 小时'));
    await waitFor(() => expect(chartWindow()).toBe('1m'));
    expect(screen.getByText('已降采样:每 1m 取最新一条')).toBeTruthy();

    // 5 分钟映射为 null:数据量小,自动模式下也走全量。
    await user.click(screen.getByText('近 5 分钟'));
    await waitFor(() => expect(chartWindow()).toBe(''));
    expect(
      screen.getByText('全量数据(当前范围数据量小,无需降采样)'),
    ).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /导出 CSV/ }));
    await waitFor(() => expect(fetchPointHistory).toHaveBeenCalled());
    expect(lastExportWindow()).toBeUndefined();
  });
});
