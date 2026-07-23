/**
 * 连接测试弹窗。
 *
 * 用户的原话是「反馈太模糊了,要等好一会才会有弹窗」。所以第一条用例锁的就是
 * **点开的瞬间就得有东西看**:请求还没回来时,四个步骤已经列出来了。
 *
 * 第二条锁的是诚实:结果没回来之前,不许把后面的步骤假装点亮 —— 后端不流式
 * 推送,前端根本不知道它走到哪一步了。
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn(),
}));

import { apiClient } from '../services/apiClient';
import ConnectionTestModal from './ConnectionTestModal';

const STEPS_OK = [
  { key: 'config', label: '检查设备配置', status: 'ok', detail: 'Modbus TCP · source_ip=10.0.0.1', duration_ms: 0.4 },
  { key: 'connect', label: '建立连接', status: 'ok', detail: '已建立连接', duration_ms: 12.5 },
  { key: 'handshake', label: '握手/健康检查', status: 'ok', detail: '设备响应正常', duration_ms: 8.1 },
  { key: 'disconnect', label: '断开连接', status: 'ok', detail: '已释放', duration_ms: 0.2 },
];

function open(props = {}) {
  return render(
    <ConnectionTestModal open deviceId={1} deviceName="1号空压机" onClose={vi.fn()} {...props} />,
  );
}

describe('打开的瞬间', () => {
  it('请求还没回来,四个步骤就已经列出来了 —— 不能干等', () => {
    // 永远不 resolve:模拟一个很慢的设备
    vi.mocked(apiClient.post).mockReturnValue(new Promise(() => undefined));
    open();

    for (const label of ['检查设备配置', '建立连接', '握手/健康检查', '断开连接']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText(/已用时/)).toBeInTheDocument();
  });

  it('结果没回来之前,后面的步骤只能是「等待中」,不许假装点亮', () => {
    vi.mocked(apiClient.post).mockReturnValue(new Promise(() => undefined));
    open();

    const handshake = screen.getByText('握手/健康检查').closest('div')?.parentElement;
    expect(within(handshake as HTMLElement).getByText('等待中')).toBeInTheDocument();
    // 「通过」是只有真结果回来才配出现的字样
    expect(screen.queryByText('通过')).not.toBeInTheDocument();
  });

  it('弹窗标题带上设备名,免得同时开几个分不清', () => {
    vi.mocked(apiClient.post).mockReturnValue(new Promise(() => undefined));
    open();
    expect(screen.getByText('连接测试 · 1号空压机')).toBeInTheDocument();
  });
});

describe('结果回来以后', () => {
  it('每一步显示真实结果与耗时,并给出总结', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data: { success: true, summary: '连接正常', protocol: 'modbus_tcp', steps: STEPS_OK, total_ms: 21.2 },
    });
    open();

    await waitFor(() => expect(screen.getByText('连接正常')).toBeInTheDocument());
    expect(screen.getAllByText('通过')).toHaveLength(4);
    expect(screen.getByText('13 ms')).toBeInTheDocument();      // 12.5 → 13
    expect(screen.getByText(/总耗时 21 ms/)).toBeInTheDocument();
    expect(screen.queryByText(/已用时/)).not.toBeInTheDocument();
  });

  it('失败时指明卡在哪一步,后续步骤如实标为已跳过', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        success: false,
        summary: '建立连接失败:Connection refused',
        protocol: 'modbus_tcp',
        total_ms: 3.0,
        steps: [
          { key: 'config', label: '检查设备配置', status: 'ok', detail: '', duration_ms: 0.3 },
          { key: 'connect', label: '建立连接', status: 'failed', detail: 'Connection refused', duration_ms: 2.4 },
          { key: 'handshake', label: '握手/健康检查', status: 'skipped', detail: '未连接,跳过', duration_ms: 0 },
          { key: 'disconnect', label: '断开连接', status: 'skipped', detail: '未连接,无需断开', duration_ms: 0 },
        ],
      },
    });
    open();

    await waitFor(() =>
      expect(screen.getByText('建立连接失败:Connection refused')).toBeInTheDocument(),
    );
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('Connection refused')).toBeInTheDocument();
    expect(screen.getAllByText('已跳过')).toHaveLength(2);
  });

  it('「连上了但设备不响应」单独标成异常 —— 和连不上不是一回事', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        success: false,
        summary: '已连接,但设备未通过健康检查',
        protocol: 'modbus_tcp',
        total_ms: 30,
        steps: [
          { key: 'config', label: '检查设备配置', status: 'ok', detail: '', duration_ms: 0.2 },
          { key: 'connect', label: '建立连接', status: 'ok', detail: '已建立连接', duration_ms: 10 },
          { key: 'handshake', label: '握手/健康检查', status: 'warning', detail: '可能是从站地址不对', duration_ms: 20 },
          { key: 'disconnect', label: '断开连接', status: 'ok', detail: '已释放', duration_ms: 0.1 },
        ],
      },
    });
    open();

    await waitFor(() => expect(screen.getByText('异常')).toBeInTheDocument());
    expect(screen.getByText('可能是从站地址不对')).toBeInTheDocument();
  });

  it('504 超时也照样把步骤画出来 —— 后端在超时分支里给了清单', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({
      response: {
        status: 504,
        data: {
          success: false,
          summary: '连接测试超时(>5s)，设备无响应或网络不可达',
          steps: [
            { key: 'config', label: '检查设备配置', status: 'ok', detail: '', duration_ms: 0 },
            { key: 'connect', label: '建立连接', status: 'failed', detail: '超过 5s 无响应', duration_ms: 5000 },
            { key: 'handshake', label: '握手/健康检查', status: 'skipped', detail: '', duration_ms: 0 },
            { key: 'disconnect', label: '断开连接', status: 'skipped', detail: '', duration_ms: 0 },
          ],
        },
      },
    });
    open();

    await waitFor(() => expect(screen.getByText(/连接测试超时/)).toBeInTheDocument());
    expect(screen.getByText('5000 ms')).toBeInTheDocument();
  });

  it('请求本身挂了(网络断/后端没起)如实报出来,而不是一直转圈', async () => {
    vi.mocked(apiClient.post).mockRejectedValue(new Error('Network Error'));
    open();

    await waitFor(() => expect(screen.getByText('请求失败')).toBeInTheDocument());
    expect(screen.getByText('Network Error')).toBeInTheDocument();
  });
});

describe('请求参数', () => {
  it('打的是该设备的 test-connection,且不让全局 toast 再喊一遍', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({
      data: { success: true, summary: '连接正常', steps: STEPS_OK, total_ms: 1 },
    });
    open({ deviceId: 42 });

    await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
    const [url, body, config] = vi.mocked(apiClient.post).mock.calls[0];
    expect(url).toBe('/config/devices/42/test-connection/');
    expect(body).toBeUndefined();
    expect(config).toMatchObject({ silent: true });
  });

  it('没打开时不发请求', () => {
    render(<ConnectionTestModal open={false} deviceId={1} onClose={vi.fn()} />);
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
