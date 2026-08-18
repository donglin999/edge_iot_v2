import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  fetchTasks,
  startTask as startAcquisitionTask,
  stopSession as stopAcquisitionSession,
  updateTaskSampleRate,
} from './acquisitionApi';
import {
  fetchPointHistory,
  startTask as startDataTask,
  stopSession as stopDataSession,
} from './dataApi';
import {
  deleteDevice,
  fetchDevices,
  testDeviceConnection,
  updateDevice,
} from './deviceApi';
import {
  bulkDeleteVersions,
  deleteVersion,
  rollbackToVersion,
} from './versionApi';

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

  it('keeps acquisition control paths, methods and payloads exact', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ session_id: 11 }))
      .mockResolvedValueOnce(
        jsonResponse({ detail: 'stopped', session_id: 11, current_status: 'stopped' }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 7, sample_rate_hz: 2 }));

    await startAcquisitionTask({ task_id: 7, worker_identifier: 'worker-a' });
    await stopAcquisitionSession(11, 'operator');
    await updateTaskSampleRate(7, 2);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/acquisition/sessions/start-task/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ task_id: 7, worker_identifier: 'worker-a' }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/acquisition/sessions/11/stop/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ reason: 'operator' }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/config/tasks/7/',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ sample_rate_hz: 2 }),
      }),
    );
  });

  it('keeps the data-page start/stop compatibility payloads distinct', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ id: 21, status: 'starting' }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'stopped' }));

    await startDataTask(9);
    await stopDataSession(21);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/acquisition/sessions/start-task/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ task_id: 9 }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/acquisition/sessions/21/stop/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ reason: '手动停止' }),
      }),
    );
  });

  it('keeps device test, update and delete routes exact', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ success: true, message: 'ok', details: {} }))
      .mockResolvedValueOnce(jsonResponse({ id: 4, name: 'new name' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await testDeviceConnection(4);
    await updateDevice(4, { name: 'new name' });
    await deleteDevice(4);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/config/devices/4/test-connection/',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/config/devices/4/',
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ name: 'new name' }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/config/devices/4/',
      { method: 'DELETE' },
    );
  });

  it('keeps version rollback and deletion contracts exact', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        jsonResponse({
          detail: 'rolled back',
          new_version_id: 8,
          new_version_number: 4,
          rollback_from_version: 3,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse({ deleted: [8], skipped: [{ id: 9, reason: 'running' }] }));

    await rollbackToVersion(3);
    await deleteVersion(8);
    await bulkDeleteVersions([8, 9]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/config/versions/3/rollback/',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/config/versions/8/',
      { method: 'DELETE' },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/config/versions/bulk-delete/',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ ids: [8, 9] }),
      }),
    );
  });
});
