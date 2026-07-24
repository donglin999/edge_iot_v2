/**
 * 设备管理页的「下载 Excel 模板」下拉重组(docs/excel-import-export-v2.md「前端」节):
 *   - 每个生产协议一项,走 v2 每协议两表模板;
 *   - scada 项保持指向既有网关两表模板;
 *   - 通用单表模板(legacy)沉底并标注;
 *   - simulator 不是生产协议,不出现在 v2 分组里。
 *
 * 以及「导出当前设备」的协议筛选联动:具体生产协议走 v2 导出,「全部」仍是
 * legacy 全量导出,scada 提示去网关页。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { message } from 'antd';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import { apiClient, downloadFile } from '../services/apiClient';
import DeviceListPage from './DeviceListPage';

const PROTOCOLS = [
  {
    name: 'modbus_tcp',
    label: 'Modbus TCP',
    category: 'industrial-ethernet' as const,
    description: '',
    supports_pause: true,
    identity_fields: [],
    device_fields: [],
    point_fields: [],
  },
  {
    name: 'scada',
    label: 'SCADA 网关 (MQTT)',
    category: 'iot' as const,
    description: '',
    supports_pause: true,
    identity_fields: [],
    device_fields: [],
    point_fields: [],
  },
  {
    name: 'simulator',
    label: '模拟器',
    category: 'other' as const,
    description: '',
    supports_pause: true,
    identity_fields: [],
    device_fields: [],
    point_fields: [],
  },
];

function stubApi() {
  vi.mocked(apiClient.get).mockImplementation((url: string) => {
    if (url.includes('/acquisition/protocols/')) return Promise.resolve({ data: PROTOCOLS });
    return Promise.resolve({ data: { count: 0, next: null, previous: null, results: [] } });
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DeviceListPage />
    </MemoryRouter>,
  );
}

async function openTemplateMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText('下载 Excel 模板'));
}

describe('模板下拉重组', () => {
  it('每个生产协议一项 v2 模板,scada 走网关模板,legacy 通用单表沉底', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await waitFor(() => expect(apiClient.get).toHaveBeenCalled());
    await openTemplateMenu(user);

    expect(await screen.findByText('Modbus TCP 模板(设备+测点两表)')).toBeInTheDocument();
    expect(screen.getByText(/SCADA 两表模板/)).toBeInTheDocument();
    expect(screen.getByText('通用单表模板(legacy · 全部协议)')).toBeInTheDocument();

    // simulator 不是生产协议,不该出现在 v2 分组里
    expect(screen.queryByText('模拟器 模板(设备+测点两表)')).not.toBeInTheDocument();
  });

  it('点击某协议的 v2 模板项,按该协议下载', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await openTemplateMenu(user);

    await user.click(await screen.findByText('Modbus TCP 模板(设备+测点两表)'));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/config/protocol-excel/template/',
        'modbus_tcp_template.xlsx',
        { protocol: 'modbus_tcp' },
      ),
    );
  });

  it('点击 legacy 通用单表模板,走旧的全协议通用端点', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await openTemplateMenu(user);

    await user.click(await screen.findByText('通用单表模板(legacy · 全部协议)'));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/acquisition/protocols/template/',
        'edge_iot_excel_template.xlsx',
        undefined,
      ),
    );
  });

  it('点击 SCADA 模板项,走既有网关两表模板端点', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await openTemplateMenu(user);

    await user.click(await screen.findByText(/SCADA 两表模板/));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/config/scada-gateways/template/',
        'scada_template.xlsx',
      ),
    );
  });
});

describe('导出联动(独立「导出配置」按钮)', () => {
  it('筛选具体生产协议时,导出走 v2 per-protocol', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await user.click(await screen.findByText(/Modbus TCP \(0\)/));

    await user.click(screen.getByRole('button', { name: /导出配置/ }));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/config/protocol-excel/export/',
        'modbus_tcp_devices.xlsx',
        { protocol: 'modbus_tcp' },
      ),
    );
  });

  it('筛选「全部」时仍走 40 列全量导出(legacy)', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();

    await user.click(screen.getByRole('button', { name: /导出配置/ }));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/config/devices/export/',
        'edge_iot_devices_export.xlsx',
        undefined,
      ),
    );
  });

  it('筛选 scada 时提示去网关页,不发导出请求', async () => {
    const warnSpy = vi.spyOn(message, 'warning').mockImplementation(() => '' as never);
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await user.click(await screen.findByText(/SCADA 网关 \(MQTT\) \(0\)/));

    await user.click(screen.getByRole('button', { name: /导出配置/ }));
    await waitFor(() => expect(warnSpy).toHaveBeenCalledWith(expect.stringMatching(/请在「SCADA 网关」页导出/)));
    expect(downloadFile).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});

describe('导入配置按钮', () => {
  it('点击后打开 v2 导入弹窗', async () => {
    const user = userEvent.setup();
    stubApi();
    renderPage();
    await user.click(await screen.findByText('导入配置'));
    expect(await screen.findByText('导入配置(Excel)')).toBeInTheDocument();
  });
});
