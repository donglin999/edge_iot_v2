import { fireEvent, render, screen, waitFor } from '@testing-library/vue';
import { defineComponent } from 'vue';

import {
  fetchDashboardActiveSessions,
  fetchDashboardDevices,
  fetchDashboardOverview,
  fetchDashboardTasks,
} from '@/services/dashboardApi';

import DashboardPage from './DashboardPage.vue';

vi.mock('@/services/dashboardApi', () => ({
  fetchDashboardActiveSessions: vi.fn(),
  fetchDashboardDevices: vi.fn(),
  fetchDashboardOverview: vi.fn(),
  fetchDashboardTasks: vi.fn(),
}));

const RouterLinkStub = defineComponent({
  name: 'RouterLink',
  props: { to: { type: String, required: true } },
  template: '<a :href="to"><slot /></a>',
});

const overview = {
  total_tasks: 4,
  active_tasks: 3,
  status: { succeeded: 1 },
  recent_runs: [
    {
      task: 'task-a',
      status: 'succeeded',
      started_at: '2026-08-18T08:30:00Z',
      finished_at: '2026-08-18T08:31:00Z',
      worker: 'worker-a',
      log_reference: 'run-a',
    },
  ],
  generated_at: '2026-08-18T08:31:00Z',
};

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

function renderPage() {
  return render(DashboardPage, {
    global: { stubs: { RouterLink: RouterLinkStub } },
  });
}

beforeEach(() => {
  vi.mocked(fetchDashboardOverview).mockResolvedValue(overview);
  vi.mocked(fetchDashboardTasks).mockResolvedValue([
    { id: 1, code: 'task-a', name: '一号任务', is_active: true },
  ]);
  vi.mocked(fetchDashboardDevices).mockResolvedValue([
    { id: 1, status: 'online' },
    { id: 2, status: 'online' },
    { id: 3, status: 'offline' },
  ]);
  vi.mocked(fetchDashboardActiveSessions).mockResolvedValue([
    { id: 1, status: 'running' },
    { id: 2, status: 'running' },
    { id: 3, status: 'stopped' },
  ]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('DashboardPage', () => {
  it('exposes the initial loading state then renders aggregated live metrics', async () => {
    const pendingOverview = deferred<typeof overview>();
    vi.mocked(fetchDashboardOverview).mockReturnValueOnce(pendingOverview.promise);

    renderPage();

    expect(screen.getByTestId('dashboard-page')).toHaveAttribute('aria-busy', 'true');
    expect(fetchDashboardOverview).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(fetchDashboardTasks).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(fetchDashboardDevices).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(fetchDashboardActiveSessions).toHaveBeenCalledWith(expect.any(AbortSignal));

    pendingOverview.resolve(overview);

    await waitFor(() => {
      expect(screen.getByTestId('dashboard-page')).toHaveAttribute('aria-busy', 'false');
    });
    expect(screen.getAllByText('task-a')).toHaveLength(2);
    expect(screen.getByText('worker-a')).toBeInTheDocument();
    expect(screen.getByText('成功')).toBeInTheDocument();
    expect(screen.getByText(
      '任务概览与最近运行：默认站点（site_code=default）；任务列表、设备与运行会话：全局接口口径。',
    )).toBeInTheDocument();
    expect(screen.getByText('全局任务列表')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-page')).toHaveTextContent('默认站点任务总数4');
    expect(screen.getByTestId('dashboard-page')).toHaveTextContent('默认站点启用任务3');
    expect(screen.getByTestId('dashboard-page')).toHaveTextContent('全局运行中会话2');
    expect(screen.getByTestId('dashboard-page')).toHaveTextContent('全局设备在线2/ 3');
  });

  it('keeps successful sections visible and identifies a failed section', async () => {
    vi.mocked(fetchDashboardDevices).mockRejectedValueOnce(new Error('offline'));

    renderPage();

    expect(await screen.findByText('部分数据加载失败：设备')).toBeInTheDocument();
    expect(screen.getAllByText('task-a')).toHaveLength(2);
    expect(screen.getByTestId('dashboard-page')).toHaveTextContent('全局设备在线0/ 0');
  });

  it('renders explicit task and run empty states', async () => {
    vi.mocked(fetchDashboardOverview).mockResolvedValueOnce({
      ...overview,
      total_tasks: 0,
      active_tasks: 0,
      recent_runs: [],
    });
    vi.mocked(fetchDashboardTasks).mockResolvedValueOnce([]);
    vi.mocked(fetchDashboardDevices).mockResolvedValueOnce([]);
    vi.mocked(fetchDashboardActiveSessions).mockResolvedValueOnce([]);

    renderPage();

    expect(
      await screen.findByText('暂无任务 —— 导入配置或在采集控制页新建'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('暂无运行记录 —— 启动采集任务后在此显示'),
    ).toBeInTheDocument();
  });

  it('refreshes every dashboard source on demand', async () => {
    renderPage();
    await screen.findAllByText('task-a');

    await fireEvent.click(screen.getByRole('button', { name: '刷新概览' }));

    await waitFor(() => expect(fetchDashboardOverview).toHaveBeenCalledTimes(2));
    expect(fetchDashboardTasks).toHaveBeenCalledTimes(2);
    expect(fetchDashboardDevices).toHaveBeenCalledTimes(2);
    expect(fetchDashboardActiveSessions).toHaveBeenCalledTimes(2);
  });
});
