/**
 * 「添加/编辑设备」弹窗 —— 按协议分发配置界面的宿主。
 *
 * 这里的第一个用例锁的是一个真实事故:从「添加设备」下拉选好 Modbus TCP,
 * 弹窗里字段也照着渲染了,但表单 store 里的 protocol 仍是空值(Modal 内容
 * 延迟挂载,打开瞬间那次 setFieldsValue 写在表单项注册之前),一按保存就报
 * 「请选择协议」—— 所有通用协议的新建全是坏的。lint 和 build 都发现不了,
 * 只有真渲染 + 真点保存才会暴露。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import { apiClient } from '../services/apiClient';
import DeviceFormModal from './DeviceFormModal';

const MODBUS_TCP = {
  name: 'modbus_tcp',
  label: 'Modbus TCP',
  category: 'industrial-ethernet' as const,
  description: '基于以太网的 Modbus,端口 502',
  supports_pause: true,
  identity_fields: ['source_ip', 'source_port'],
  device_fields: [
    {
      name: 'source_ip',
      label: 'IP 地址',
      kind: 'string' as const,
      required: true,
      default: null,
      choices: null,
      help_text: '',
      example: '192.168.1.100',
    },
    {
      name: 'source_port',
      label: '端口',
      kind: 'int' as const,
      required: false,
      default: 502,
      choices: null,
      help_text: '',
      example: 502,
    },
    {
      name: 'timeout',
      label: '超时(秒)',
      kind: 'float' as const,
      required: false,
      default: 10,
      choices: null,
      help_text: '',
      example: 10,
    },
  ],
  point_fields: [],
};

/** 协议描述符走 GET /acquisition/protocols/,其余 GET 各自返回。 */
function stubApi(overrides: Record<string, unknown> = {}) {
  vi.mocked(apiClient.get).mockImplementation((url: string) => {
    if (url.includes('/acquisition/protocols/')) {
      return Promise.resolve({ data: [MODBUS_TCP] });
    }
    const hit = Object.entries(overrides).find(([key]) => url.includes(key));
    if (hit) return Promise.resolve({ data: hit[1] });
    return Promise.resolve({ data: [] });
  });
}

const save = () => screen.getByRole('button', { name: /保\s*存/ });

describe('新建设备(通用协议)', () => {
  it('从下拉预选的协议必须真的进到表单里,否则保存会报「请选择协议」', async () => {
    const user = userEvent.setup();
    stubApi();
    vi.mocked(apiClient.post).mockResolvedValue({ data: { id: 1 } });

    render(
      <DeviceFormModal open deviceId={undefined} defaultProtocol="modbus_tcp" onClose={vi.fn()} />,
    );

    // 协议选择框显示的是预选的协议,而不是 placeholder
    await waitFor(() => {
      expect(screen.getByText(/Modbus TCP/)).toBeInTheDocument();
    });

    await user.type(screen.getByPlaceholderText(/1#车间空压机/), '1号空压机');
    await user.type(screen.getByPlaceholderText('192.168.1.100'), '10.0.0.5');
    await user.click(save());

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledTimes(1);
    });
    const [url, payload] = vi.mocked(apiClient.post).mock.calls[0];
    expect(url).toBe('/config/devices/');
    expect(payload).toMatchObject({
      name: '1号空压机',
      protocol: 'modbus_tcp',
      ip_address: '10.0.0.5',
    });
    // code 由 identity_fields 拼出,后端要求唯一
    expect((payload as { code: string }).code).toBe('modbus_tcp-10.0.0.5-502');
    // 没有任何校验错误留在界面上
    expect(screen.queryByText('请选择协议')).not.toBeInTheDocument();
  });

  it('通用表单按元数据分组渲染,有默认值的字段收进折叠的高级选项', async () => {
    stubApi();
    render(<DeviceFormModal open defaultProtocol="modbus_tcp" onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('连接参数')).toBeInTheDocument());
    expect(screen.getByText('高级选项')).toBeInTheDocument();
    // 宿主负责的公共字段仍在(scada 才由组件自己接管)
    expect(screen.getByText('设备名称')).toBeInTheDocument();
    expect(screen.getByText('站点 ID')).toBeInTheDocument();
  });

  it('缺必填项时不发请求,把错误留在弹窗里', async () => {
    const user = userEvent.setup();
    stubApi();
    const onClose = vi.fn();
    render(<DeviceFormModal open defaultProtocol="modbus_tcp" onClose={onClose} />);

    await waitFor(() => expect(screen.getByText('连接参数')).toBeInTheDocument());
    await user.click(save());

    await waitFor(() => expect(screen.getByText('请输入设备名称')).toBeInTheDocument());
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('编辑设备', () => {
  const device = {
    id: 42,
    site: 1,
    name: '老设备',
    code: 'modbus_tcp-10.0.0.9-502',
    protocol: 'modbus_tcp',
    ip_address: '10.0.0.9',
    port: 502,
    metadata: { source_ip: '10.0.0.9', source_port: 502, timeout: 30 },
  };

  it('回填已有配置,并禁止中途换协议(换了等于换一套配置界面)', async () => {
    stubApi({ '/config/devices/42/': device });
    render(<DeviceFormModal open deviceId={42} onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByDisplayValue('老设备')).toBeInTheDocument());
    expect(screen.getByDisplayValue('10.0.0.9')).toBeInTheDocument();

    const protocolBox = screen.getByText(/Modbus TCP/).closest('.ant-select');
    expect(protocolBox).toHaveClass('ant-select-disabled');
  });

  it('保存走 PATCH 而不是 POST —— 否则每次编辑都会多建一台设备', async () => {
    const user = userEvent.setup();
    stubApi({ '/config/devices/42/': device });
    vi.mocked(apiClient.patch).mockResolvedValue({ data: device });

    render(<DeviceFormModal open deviceId={42} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByDisplayValue('老设备')).toBeInTheDocument());
    await user.click(save());

    await waitFor(() => expect(apiClient.patch).toHaveBeenCalledTimes(1));
    expect(vi.mocked(apiClient.patch).mock.calls[0][0]).toBe('/config/devices/42/');
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('编辑态高级选项里有改过默认值的字段时自动展开', async () => {
    stubApi({ '/config/devices/42/': device });
    render(<DeviceFormModal open deviceId={42} onClose={vi.fn()} />);

    // timeout 默认 10,设备上是 30 → 该展开,能直接看到自己改过的值
    await waitFor(() => expect(screen.getByText('高级选项')).toBeInTheDocument());
    const advanced = screen.getByText('高级选项').closest('.ant-collapse-item') as HTMLElement;
    expect(advanced.querySelector('.ant-collapse-header')).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(within(advanced).getByDisplayValue('30.0')).toBeInTheDocument();
  });
});
