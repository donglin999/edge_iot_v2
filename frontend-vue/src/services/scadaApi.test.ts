/**
 * scada 网关 API 的契约测试。
 *
 * 重点在两处「后端做好了、前端一直没接上」的地方:两表 Excel 的下载/导出/导入。
 * 这三个端点从建好起就没有任何前端调用方,等于死代码 —— 这里把 URL、
 * multipart 字段名、以及「没填任务编码就不许带任务字段」钉死。
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('./apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import { apiClient, downloadFile } from './apiClient';
import {
  downloadScadaTemplate,
  exportGateway,
  extractImportErrors,
  importScadaExcel,
} from './scadaApi';

const xlsx = () =>
  new File(['x'], 'a.xlsx', {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });

describe('两表 Excel 的下载与导出', () => {
  it('模板走网关自己的 template 端点,而不是通用的 40 列单表模板', async () => {
    await downloadScadaTemplate();
    const [url] = vi.mocked(downloadFile).mock.calls[0]!;
    expect(url).toBe('/config/scada-gateways/template/');
    expect(url).not.toContain('/acquisition/protocols/template/');
  });

  it('导出带上网关 id,文件名里的斜杠/空格被换掉(否则浏览器存不下来)', async () => {
    await exportGateway(7, 'zs/gw 1');
    const [url, filename] = vi.mocked(downloadFile).mock.calls[0]!;
    expect(url).toBe('/config/scada-gateways/7/export/');
    expect(filename).toBe('scada_zs_gw_1.xlsx');
  });
});

describe('importScadaExcel', () => {
  it('用 multipart 字段名 file 上传,并且不弹全局错误(逐行错误由界面渲染)', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: { created: {} } });
    await importScadaExcel(xlsx());

    const [url, body, config] = vi.mocked(apiClient.post).mock.calls[0]!;
    expect(url).toBe('/config/scada-gateways/import/');
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get('file')).toBeInstanceOf(File);
    expect(config).toMatchObject({ silent: true });
  });

  it('不能带上 apiClient 默认的 application/json —— 那会被 DRF 拒成 415', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: {} });
    await importScadaExcel(xlsx());

    const config = vi.mocked(apiClient.post).mock.calls[0]![2] as {
      headers?: Record<string, unknown>;
    };
    // 显式置空,好让 axios/浏览器按 FormData 生成带 boundary 的 multipart 头
    expect(config.headers).toHaveProperty('Content-Type', undefined);
  });

  it('没填任务编码时一个任务字段都不带 —— 免得后端建出个空任务', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: {} });
    await importScadaExcel(xlsx(), { task_name: '只填了名字', sample_rate_hz: 2 });

    const body = vi.mocked(apiClient.post).mock.calls[0]![1] as FormData;
    expect(body.get('task_code')).toBeNull();
    expect(body.get('task_name')).toBeNull();
    expect(body.get('sample_rate_hz')).toBeNull();
  });

  it('填了任务编码则任务字段一起带上', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: {} });
    await importScadaExcel(xlsx(), {
      task_code: 'task-zs',
      task_name: '中山采集',
      sample_rate_hz: 2,
    });

    const body = vi.mocked(apiClient.post).mock.calls[0]![1] as FormData;
    expect(body.get('task_code')).toBe('task-zs');
    expect(body.get('task_name')).toBe('中山采集');
    expect(body.get('sample_rate_hz')).toBe('2');
  });
});

describe('extractImportErrors', () => {
  it('把逐行错误渲染成「第 N 行 · 列名:说明」,而不是丢一句「请求失败」', () => {
    const error = {
      response: {
        data: {
          errors: [
            { row: 3, column: 'code', message: '测点编码不能为空', protocol: 'scada' },
            { row: 5, column: 'device_name', message: '设备名重复', protocol: 'scada' },
          ],
        },
      },
    };
    expect(extractImportErrors(error)).toEqual([
      '第 3 行 · code:测点编码不能为空',
      '第 5 行 · device_name:设备名重复',
    ]);
  });

  it('没有逐行错误时回落到通用压平(比如 500 或网络错)', () => {
    expect(extractImportErrors({ response: { data: { detail: '服务器开小差' } } })).toEqual([
      '服务器开小差',
    ]);
    expect(extractImportErrors(new Error('Network Error'))).toEqual(['Network Error']);
  });
});
