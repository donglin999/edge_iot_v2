import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./apiClient', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

import { apiClient } from './apiClient';
import {
  createPoint,
  createTask,
  deletePoint,
  deleteTask,
  fetchDevicePoints,
  fetchTask,
  updatePoint,
  updateTask,
} from './taskApi';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('task API route contract', () => {
  it('keeps task CRUD on the existing /config/tasks paths', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { id: 7 } });
    vi.mocked(apiClient.post).mockResolvedValue({ data: { id: 8 } });
    vi.mocked(apiClient.patch).mockResolvedValue({ data: { id: 7 } });
    vi.mocked(apiClient.delete).mockResolvedValue({ data: undefined });

    await fetchTask(7);
    await createTask({ code: 'task-8', points: [1, 2] });
    await updateTask(7, { sample_rate_hz: 2 });
    await deleteTask(7);

    expect(apiClient.get).toHaveBeenCalledWith('/config/tasks/7/');
    expect(apiClient.post).toHaveBeenCalledWith('/config/tasks/', {
      code: 'task-8',
      points: [1, 2],
    });
    expect(apiClient.patch).toHaveBeenCalledWith('/config/tasks/7/', {
      sample_rate_hz: 2,
    });
    expect(apiClient.delete).toHaveBeenCalledWith('/config/tasks/7/');
  });

  it('keeps point CRUD and the device points action on their exact paths', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { results: [{ id: 3 }] } });
    vi.mocked(apiClient.post).mockResolvedValue({ data: { id: 4 } });
    vi.mocked(apiClient.patch).mockResolvedValue({ data: { id: 4 } });
    vi.mocked(apiClient.delete).mockResolvedValue({ data: undefined });

    await expect(fetchDevicePoints(9)).resolves.toEqual([{ id: 3 }]);
    await createPoint({ device: 9, code: 'temperature' });
    await updatePoint(4, { description: '温度' });
    await deletePoint(4);

    expect(apiClient.get).toHaveBeenCalledWith('/config/devices/9/points/');
    expect(apiClient.post).toHaveBeenCalledWith('/config/points/', {
      device: 9,
      code: 'temperature',
    });
    expect(apiClient.patch).toHaveBeenCalledWith('/config/points/4/', {
      description: '温度',
    });
    expect(apiClient.delete).toHaveBeenCalledWith('/config/points/4/');
  });
});
