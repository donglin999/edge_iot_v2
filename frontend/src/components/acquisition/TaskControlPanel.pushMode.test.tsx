/**
 * 推模式任务面板:scada/mqtt 任务没有「采样频率」概念 —— 不渲染频率编辑控件,
 * 改显示「推送驱动 · 实时消费」;拉模式协议(modbus 等)保持频率编辑器。
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { App as AntdApp } from 'antd';

import TaskControlPanel from './TaskControlPanel';
import type { AcqTask } from '../../services/acquisitionApi';

vi.mock('../../services/acquisitionApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/acquisitionApi')>();
  return {
    ...actual,
    startTask: vi.fn(),
    stopSession: vi.fn(),
    updateTaskSampleRate: vi.fn(),
  };
});
vi.mock('../../services/taskApi', () => ({ deleteTask: vi.fn() }));
vi.mock('../../hooks/useWebSocket', () => ({
  useWebSocket: () => ({ status: 'disconnected' }),
  WebSocketMessage: {},
}));

function makeTask(overrides: Partial<AcqTask>): AcqTask {
  return {
    id: 1,
    code: 'task-x',
    name: '任务X',
    description: '',
    schedule: '',
    is_active: true,
    sample_rate_hz: 1,
    created_at: '',
    updated_at: '',
    ...overrides,
  } as AcqTask;
}

function renderPanel(task: AcqTask) {
  return render(
    <AntdApp>
      <TaskControlPanel task={task} onStatusChange={() => undefined} />
    </AntdApp>,
  );
}

describe('推模式任务面板', () => {
  it('scada 任务:显示推送驱动,不渲染采样频率编辑器', () => {
    renderPanel(makeTask({ device_protocol: 'scada' }));
    expect(screen.getByText('推送驱动 · 实时消费')).toBeInTheDocument();
    expect(screen.queryByText('采样频率')).not.toBeInTheDocument();
  });

  it('mqtt 任务同样是推送驱动', () => {
    renderPanel(makeTask({ device_protocol: 'mqtt' }));
    expect(screen.getByText('推送驱动 · 实时消费')).toBeInTheDocument();
  });

  it('modbus 任务保留采样频率编辑器', () => {
    renderPanel(makeTask({ device_protocol: 'modbus_tcp' }));
    expect(screen.getByText('采样频率')).toBeInTheDocument();
    expect(screen.queryByText('推送驱动 · 实时消费')).not.toBeInTheDocument();
  });

  it('无测点任务(device_protocol=null)按拉模式兜底显示频率编辑器', () => {
    renderPanel(makeTask({ device_protocol: null }));
    expect(screen.getByText('采样频率')).toBeInTheDocument();
  });
});
