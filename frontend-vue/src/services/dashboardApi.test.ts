import {
  fetchDashboardActiveSessions,
  fetchDashboardDevices,
  fetchDashboardOverview,
  fetchDashboardTasks,
} from './dashboardApi';

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

afterEach(() => {
  vi.restoreAllMocks();
});

describe('dashboard API', () => {
  it('keeps overview and active-session URLs exact and forwards AbortSignal', async () => {
    const controller = new AbortController();
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        jsonResponse({
          total_tasks: 3,
          active_tasks: 2,
          status: {},
          recent_runs: [],
          generated_at: '2026-08-18T00:00:00Z',
        }),
      )
      .mockResolvedValueOnce(jsonResponse([{ id: 7, status: 'running' }]));

    await fetchDashboardOverview(controller.signal);
    await fetchDashboardActiveSessions(controller.signal);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/config/tasks/overview/?site_code=default',
      { signal: controller.signal },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/acquisition/sessions/active/',
      { signal: controller.signal },
    );
  });

  it('walks every task page without dropping the site filter', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        jsonResponse({
          count: 2,
          next: '/api/config/tasks/?limit=1000&offset=1',
          previous: null,
          results: [{ id: 1, code: 'task-1', name: '一号任务', is_active: true }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          count: 2,
          next: null,
          previous: '/api/config/tasks/?limit=1000&offset=0',
          results: [{ id: 2, code: 'task-2', name: '二号任务', is_active: false }],
        }),
      );

    await expect(fetchDashboardTasks()).resolves.toHaveLength(2);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/config/tasks/?site_code=default&limit=1000&offset=0',
      { signal: undefined },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/config/tasks/?site_code=default&limit=1000&offset=1',
      { signal: undefined },
    );
  });

  it('loads all devices from the unfiltered device endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        count: 2,
        next: null,
        previous: null,
        results: [
          { id: 1, status: 'online' },
          { id: 2, status: 'offline' },
        ],
      }),
    );

    await expect(fetchDashboardDevices()).resolves.toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/config/devices/?limit=1000&offset=0',
      { signal: undefined },
    );
  });

  it('surfaces a useful failure instead of changing the response contract', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('upstream unavailable', {
        status: 503,
        statusText: 'Service Unavailable',
      }),
    );

    await expect(fetchDashboardOverview()).rejects.toThrow(
      '获取任务概览失败: upstream unavailable',
    );
  });
});
