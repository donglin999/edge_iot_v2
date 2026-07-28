/**
 * 数据可视化页——测点卡片排序(team-prompts scada-topic-and-storage / Agent C):
 *   - 默认「最近更新优先」:按最新值时间戳倒序,从未有数据(timestamp 为 null)沉底;
 *   - 排序选择器可切「测点编码」(自然序)与「中文名称」(localeCompare zh);
 *   - 重排只发生在渲染层,已选中的测点(卡片高亮)不因重排丢失。
 *
 * latest-values 返回故意乱序,断言的是「渲染出来的卡片顺序」而非内部数组。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/acquisitionApi', () => ({
  fetchTasks: vi.fn(),
}));
vi.mock('../services/dataApi', () => ({
  fetchPointsLatestValues: vi.fn(),
  fetchPointHistory: vi.fn(),
}));
// 历史面板的图表(recharts)与本页排序无关,mock 掉避免 jsdom 里画图。
vi.mock('../components/PointChart', () => ({ default: () => null }));

import { fetchTasks } from '../services/acquisitionApi';
import { fetchPointsLatestValues } from '../services/dataApi';
import type { PointLatestValue } from '../services/dataApi';
import DataVisualizationPage from './DataVisualizationPage';

const TASKS = [
  { id: 1, code: 'T1', name: '任务一', is_active: true },
];

// 接口返回顺序故意乱:旧的在前、最新的在最后、还有一个从未有过数据的。
// 名称取拼音首字母可稳定区分的:电流(dian) < 温度(wen) < 压力(ya)。
const POINTS: PointLatestValue[] = [
  {
    point_code: 'P2',
    point_name: '压力',
    unit: 'kPa',
    data_type: 'float',
    device_id: 1,
    device_name: '设备A',
    value: 1.5,
    quality: 'good',
    timestamp: '2026-07-28T10:00:00Z', // 较旧
  },
  {
    point_code: 'P10',
    point_name: '电流',
    unit: 'A',
    data_type: 'float',
    device_id: 1,
    device_name: '设备A',
    value: null,
    quality: 'bad',
    timestamp: null, // 从未有过数据 → 沉底
  },
  {
    point_code: 'P1',
    point_name: '温度',
    unit: '℃',
    data_type: 'float',
    device_id: 1,
    device_name: '设备A',
    value: 23.4,
    quality: 'good',
    timestamp: '2026-07-28T12:00:00Z', // 最新
  },
];

function stubApi(points: PointLatestValue[] = POINTS) {
  vi.mocked(fetchTasks).mockResolvedValue(TASKS as never);
  vi.mocked(fetchPointsLatestValues).mockResolvedValue({
    filter: { task_id: null, device_id: null, point_code: null },
    count: points.length,
    points,
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DataVisualizationPage />
    </MemoryRouter>,
  );
}

/** 按 DOM 顺序取所有卡片上的测点编码。 */
function renderedCodes(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('.point-value-card__code')).map(
    (el) => el.textContent ?? '',
  );
}

async function pickSort(user: ReturnType<typeof userEvent.setup>, label: string) {
  // 排序选择器当前显示的选中项(唯一一个 combobox 旁的 selection item)。
  await user.click(screen.getByText('最近更新优先'));
  await user.click(await screen.findByTitle(label));
}

describe('测点卡片排序', () => {
  it('默认「最近更新优先」:时间戳倒序,null 沉底', async () => {
    stubApi();
    const { container } = renderPage();

    await waitFor(() =>
      expect(renderedCodes(container)).toEqual(['P1', 'P2', 'P10']),
    );
  });

  it('切到「测点编码」:自然序(P1 < P2 < P10)', async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderPage();
    await waitFor(() => expect(renderedCodes(container)).toHaveLength(3));

    await pickSort(user, '测点编码');
    await waitFor(() =>
      expect(renderedCodes(container)).toEqual(['P1', 'P2', 'P10']),
    );
  });

  it('切到「中文名称」:拼音序(电流 < 温度 < 压力)', async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderPage();
    await waitFor(() => expect(renderedCodes(container)).toHaveLength(3));

    await pickSort(user, '中文名称');
    await waitFor(() =>
      expect(renderedCodes(container)).toEqual(['P10', 'P1', 'P2']),
    );
  });

  it('切换排序不丢已选中的测点(卡片高亮保持)', async () => {
    const user = userEvent.setup();
    stubApi();
    const { container } = renderPage();
    await waitFor(() => expect(renderedCodes(container)).toHaveLength(3));

    // 点击 P2(压力)卡片选中它
    await user.click(screen.getByText('P2'));
    await waitFor(() =>
      expect(
        container.querySelector('.point-value-card--active .point-value-card__code')
          ?.textContent,
      ).toBe('P2'),
    );

    await pickSort(user, '中文名称');
    await waitFor(() =>
      expect(renderedCodes(container)).toEqual(['P10', 'P1', 'P2']),
    );
    // 重排后选中态仍在 P2 上
    expect(
      container.querySelector('.point-value-card--active .point-value-card__code')
        ?.textContent,
    ).toBe('P2');
  });
});
