import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchTasks } from './acquisitionApi';
import { fetchPointHistory } from './dataApi';
import { fetchDevices } from './deviceApi';

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

afterEach(() => {
  vi.restoreAllMocks();
});

describe('framework-neutral fetch services', () => {
  it('walks every DRF limit/offset page when loading devices', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        jsonResponse({
          count: 2,
          next: '/api/config/devices/?limit=1000&offset=1',
          previous: null,
          results: [{ id: 1, code: 'd-1' }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          count: 2,
          next: null,
          previous: '/api/config/devices/?limit=1000&offset=0',
          results: [{ id: 2, code: 'd-2' }],
        }),
      );

    await expect(fetchDevices()).resolves.toMatchObject([
      { id: 1, code: 'd-1' },
      { id: 2, code: 'd-2' },
    ]);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/config/devices/?limit=1000&offset=0',
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/config/devices/?limit=1000&offset=1',
    );
  });

  it('threads AbortSignal through acquisition task requests', async () => {
    const controller = new AbortController();
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse([]));

    await expect(fetchTasks(controller.signal)).resolves.toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/config/tasks/?limit=1000&offset=0',
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it('preserves history filters used by the existing backend contract', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        point_code: 'temperature',
        start_time: null,
        end_time: null,
        count: 0,
        data: [],
      }),
    );

    await fetchPointHistory(
      'temperature',
      '2026-08-18T00:00:00Z',
      '2026-08-18T01:00:00Z',
      500,
      undefined,
      '10s',
      true,
    );

    const [url] = fetchMock.mock.calls[0]!;
    const parsed = new URL(String(url), 'http://localhost');
    expect(parsed.pathname).toBe('/api/acquisition/sessions/point-history/');
    expect(Object.fromEntries(parsed.searchParams)).toEqual({
      point_code: 'temperature',
      limit: '500',
      full: '1',
      start_time: '2026-08-18T00:00:00Z',
      end_time: '2026-08-18T01:00:00Z',
      window: '10s',
    });
  });

  it('surfaces the existing HTTP error instead of changing its envelope', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('', { status: 503, statusText: 'Service Unavailable' }),
    );

    await expect(fetchDevices()).rejects.toThrow(
      '获取设备列表失败: Service Unavailable',
    );
  });
});
