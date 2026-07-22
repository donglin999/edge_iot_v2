/**
 * scada 专属配置界面。
 *
 * 两个用例对应两次真实的「保存按钮点不动」:
 *  1. 「同时创建采集任务」原本默认开启,而任务编码必填且为空 —— 每次新建设备
 *     都要先撞一次校验失败。
 *  2. 两表 Excel 的三个后端端点建好后一直没有任何前端入口,等于死代码。
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createRef } from 'react';
import { Form } from 'antd';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import { apiClient, downloadFile } from '../../services/apiClient';
import ScadaConfig from './ScadaConfig';
import type { ProtocolConfigHandle } from '../registry';

const GATEWAY = {
  id: 1,
  code: 'zhongshan-gw',
  name: '中山小家电网关',
  source_ip: '10.134.14.147',
  source_port: 8883,
  mqtt_use_tls: true,
  mqtt_username: 'ZYY_XJDZS',
  mqtt_password: 'secret',
  mqtt_qos: 0,
  mqtt_client_id: '',
  mqtt_read_timeout: 5,
  product_key: '123daffb91264286adcdf3bfe55194c7',
  topic_template: '/sys/{product_key}/device/{device_name}/thing/property/{code}/post',
  device_count: 2,
  created_at: '',
  updated_at: '',
};

const DESCRIPTOR = {
  name: 'scada',
  label: 'SCADA 网关 (MQTT)',
  category: 'iot' as const,
  description: '每个测点一个话题',
  supports_pause: true,
  identity_fields: ['source_ip', 'source_port'],
  device_fields: [],
  point_fields: [],
};

function stubApi() {
  vi.mocked(apiClient.get).mockImplementation((url: string) => {
    if (url.includes('scada-gateways')) return Promise.resolve({ data: [GATEWAY] });
    return Promise.resolve({ data: [] });
  });
}

/** 把组件挂在一个 Form 里(真实使用中它就活在 DeviceFormModal 的 Form 内)。 */
function renderConfig(props: Partial<Parameters<typeof ScadaConfig>[0]> = {}) {
  const ref = createRef<ProtocolConfigHandle>();
  const onSaved = vi.fn();
  const onRequestClose = vi.fn();
  const Harness = () => {
    const [form] = Form.useForm();
    return (
      <Form form={form} layout="vertical">
        <ScadaConfig
          ref={ref}
          protocol="scada"
          descriptor={DESCRIPTOR}
          mode="create"
          form={form}
          onSaved={onSaved}
          onRequestClose={onRequestClose}
          {...props}
        />
      </Form>
    );
  };
  render(<Harness />);
  return { ref, onSaved, onRequestClose };
}

describe('新建 scada 设备', () => {
  it('采集任务默认关闭 —— 不填任务编码也能直接保存', async () => {
    const user = userEvent.setup();
    stubApi();
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        gateway: 1,
        site: 1,
        devices: [{ id: 9, device_name: 'A001', code: 'scada-zhongshan-gw-A001', points: [] }],
        task: null,
        created: { devices: 1, points: 1 },
      },
    });

    const { ref, onSaved } = renderConfig();
    await waitFor(() => expect(screen.getByText(/连接参数已由网关提供/)).toBeInTheDocument());

    // 任务编码输入框根本不该出现(开关是关的)
    expect(screen.queryByPlaceholderText(/task-zhongshan/)).not.toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(/A0201010001150403/), 'A0201010001150403');
    await user.type(screen.getAllByPlaceholderText(/N270400150027/)[0], 'N270400150027');

    await waitFor(async () => {
      expect(await ref.current?.submit()).toBe(true);
    });

    const [url, payload] = vi.mocked(apiClient.post).mock.calls[0];
    expect(url).toBe('/config/scada-gateways/1/provision/');
    expect(payload).toMatchObject({
      devices: [{ device_name: 'A0201010001150403', points: [{ code: 'N270400150027' }] }],
    });
    // 没勾任务就不许带 task,否则后端会建出个空任务
    expect(payload).not.toHaveProperty('task');
    expect(onSaved).toHaveBeenCalled();
  });

  it('什么都没填时不发请求 —— 空行是编辑期占位,不算配置错误', async () => {
    stubApi();
    const { ref, onSaved } = renderConfig();
    await waitFor(() => expect(screen.getByText(/连接参数已由网关提供/)).toBeInTheDocument());

    expect(await ref.current?.submit()).toBe(false);
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText('至少需要一台设备')).toBeInTheDocument());
  });

  it('填了测点却没填设备名 → 明确指出是设备名的问题,而不是笼统报错', async () => {
    const user = userEvent.setup();
    stubApi();
    const { ref } = renderConfig();
    await waitFor(() => expect(screen.getByText(/连接参数已由网关提供/)).toBeInTheDocument());

    await user.type(screen.getAllByPlaceholderText(/N270400150027/)[0], 'N270400150027');
    expect(await ref.current?.submit()).toBe(false);

    await waitFor(() =>
      expect(screen.getByText(/设备名\(device_name\)不能为空/)).toBeInTheDocument(),
    );
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});

describe('两表 Excel', () => {
  it('下载模板走网关端点,不是设备管理页那个 40 列通用模板', async () => {
    const user = userEvent.setup();
    stubApi();
    renderConfig();
    await waitFor(() => expect(screen.getByText('下载 Excel 模板')).toBeInTheDocument());

    await user.click(screen.getByText('下载 Excel 模板'));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/config/scada-gateways/template/',
        expect.any(String),
      ),
    );
  });

  it('导出按钮在选中网关后可用,导出的是当前这个网关', async () => {
    const user = userEvent.setup();
    stubApi();
    renderConfig();
    await waitFor(() => expect(screen.getByText(/连接参数已由网关提供/)).toBeInTheDocument());

    await user.click(screen.getByText('导出当前网关'));
    await waitFor(() =>
      expect(downloadFile).toHaveBeenCalledWith(
        '/config/scada-gateways/1/export/',
        'scada_zhongshan-gw.xlsx',
      ),
    );
  });

  it('导入成功后直接关弹窗并刷新列表(配置已经全写好了,不必再按保存)', async () => {
    stubApi();
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        gateway: 1,
        site: 1,
        devices: [{ id: 9, device_name: 'A001', code: 'c', points: [] }],
        task: null,
        created: { devices: 1, points: 4 },
      },
    });

    const { onSaved, onRequestClose } = renderConfig();
    await waitFor(() => expect(screen.getByText('导入 Excel')).toBeInTheDocument());

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(['x'], 'cfg.xlsx'));

    await waitFor(() => expect(onRequestClose).toHaveBeenCalled());
    expect(vi.mocked(apiClient.post).mock.calls[0][0]).toBe('/config/scada-gateways/import/');
    expect(onSaved).toHaveBeenCalled();
  });

  it('导入失败时逐行报错、不关弹窗 —— 后端是一个事务,没写进任何东西', async () => {
    stubApi();
    vi.mocked(apiClient.post).mockRejectedValue({
      response: {
        data: {
          errors: [{ row: 4, column: 'code', message: '测点编码不能为空', protocol: 'scada' }],
        },
      },
    });

    const { onSaved, onRequestClose } = renderConfig();
    await waitFor(() => expect(screen.getByText('导入 Excel')).toBeInTheDocument());

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, new File(['x'], 'cfg.xlsx'));

    await waitFor(() =>
      expect(screen.getByText('第 4 行 · code:测点编码不能为空')).toBeInTheDocument(),
    );
    expect(onRequestClose).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });
});
