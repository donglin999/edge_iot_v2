/**
 * v2 协议 Excel 导入弹窗(docs/excel-import-export-v2.md)。
 *
 * 三条路径对应三种真实结果:
 *  1. 导入成功 —— created/updated 摘要要显示,且要通知宿主刷新列表。
 *  2. 行级错误 —— 后端是一个事务,失败时没写任何东西,所以逐行列出来让用户
 *     去改源文件,不刷新列表。
 *  3. legacy 40 列旧文件 —— 不是把一堆「缺 sheet」之类没意义的错误甩给用户,
 *     而是识别出来后指路去「导入作业」页(旧流程原样保留)。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import { apiClient } from '../services/apiClient';
import ProtocolExcelImportModal from './ProtocolExcelImportModal';

function renderModal(props: Partial<Parameters<typeof ProtocolExcelImportModal>[0]> = {}) {
  const onClose = vi.fn();
  const onImported = vi.fn();
  render(
    <MemoryRouter>
      <ProtocolExcelImportModal open onClose={onClose} onImported={onImported} {...props} />
    </MemoryRouter>,
  );
  return { onClose, onImported };
}

async function uploadFile(file: File = new File(['x'], 'cfg.xlsx')) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input, file);
}

describe('导入成功', () => {
  it('显示 created/updated 摘要并通知宿主刷新列表', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        protocol: 'modbus_tcp',
        created: { devices: 2, points: 8, tasks: 2 },
        updated: { devices: 0, points: 0, tasks: 0 },
        devices: [{ code: 'modbus_tcp-1', points: ['p1'], task: 'task-modbus_tcp-1' }],
        errors: [],
      },
    });

    const { onImported } = renderModal();
    await uploadFile();

    await waitFor(() => expect(screen.getByText(/导入成功 —— modbus_tcp/)).toBeInTheDocument());
    expect(screen.getByText(/新建:2 台设备 \/ 8 个测点 \/ 2 个任务/)).toBeInTheDocument();
    expect(onImported).toHaveBeenCalled();

    const [url, body, config] = vi.mocked(apiClient.post).mock.calls[0];
    expect(url).toBe('/config/protocol-excel/import/');
    expect(body).toBeInstanceOf(FormData);
    expect((config as { silent?: boolean })?.silent).toBe(true);
  });
});

describe('行级错误', () => {
  it('逐行列出「sheet · 第 N 行 · 列:说明」,不通知宿主刷新', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({
      response: {
        data: {
          errors: [{ sheet: '测点', row: 4, column: 'code', message: '测点编码不能为空' }],
        },
      },
    });

    const { onImported } = renderModal();
    await uploadFile();

    await waitFor(() =>
      expect(screen.getByText('测点 · 第 4 行 · code:测点编码不能为空')).toBeInTheDocument(),
    );
    expect(screen.getByText('导入失败 —— 未写入任何数据')).toBeInTheDocument();
    expect(onImported).not.toHaveBeenCalled();
  });

  it('多行错误逐条列出,不折叠成一条', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({
      response: {
        data: {
          errors: [
            { sheet: '设备', row: 2, column: 'device_name', message: '设备名重复' },
            { sheet: '测点', row: 5, column: 'code', message: '引用了未知设备' },
          ],
        },
      },
    });

    renderModal();
    await uploadFile();

    await waitFor(() => expect(screen.getByText('设备 · 第 2 行 · device_name:设备名重复')).toBeInTheDocument());
    expect(screen.getByText('测点 · 第 5 行 · code:引用了未知设备')).toBeInTheDocument();
  });
});

describe('legacy 格式', () => {
  it('识别出旧版 40 列文件时提示去「导入作业」页,而不是甩行级错误', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({
      response: { data: { detail: '无法识别协议格式,这份文件像是旧版 40 列通用模板' } },
    });

    const { onImported } = renderModal();
    await uploadFile();

    await waitFor(() =>
      expect(screen.getByText(/这份文件像是旧版 40 列通用模板/)).toBeInTheDocument(),
    );
    const link = screen.getByText('前往「导入作业」页 →');
    expect(link.closest('a')).toHaveAttribute('href', '/import');
    // 命中 legacy 提示时不该同时把一堆行级/通用错误也甩出来
    expect(screen.queryByText('导入失败 —— 未写入任何数据')).not.toBeInTheDocument();
    expect(onImported).not.toHaveBeenCalled();
  });
});
