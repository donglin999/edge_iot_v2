import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import { apiClient, downloadFile } from './apiClient';
import {
  downloadProtocolTemplateV2,
  exportProtocolDevices,
  exportSingleDevice,
  getProtocol,
  importProtocolWorkbook,
  listProtocols,
} from './protocolApi';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('protocol registry and v2 Excel route contract', () => {
  it('uses the existing protocol registry detail and list paths', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: [] });
    await listProtocols();
    await getProtocol('modbus_tcp');

    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/acquisition/protocols/');
    expect(apiClient.get).toHaveBeenNthCalledWith(
      2,
      '/acquisition/protocols/modbus_tcp/',
    );
  });

  it('keeps v2 template and export query names stable', async () => {
    await downloadProtocolTemplateV2('mqtt');
    await exportProtocolDevices('opcua');
    await exportSingleDevice(12, 'line/a 1');

    expect(downloadFile).toHaveBeenNthCalledWith(
      1,
      '/config/protocol-excel/template/',
      'mqtt_template.xlsx',
      { protocol: 'mqtt' },
    );
    expect(downloadFile).toHaveBeenNthCalledWith(
      2,
      '/config/protocol-excel/export/',
      'opcua_devices.xlsx',
      { protocol: 'opcua' },
    );
    expect(downloadFile).toHaveBeenNthCalledWith(
      3,
      '/config/protocol-excel/export/',
      'line_a_1.xlsx',
      { device_ids: '12' },
    );
  });

  it('uploads the workbook under multipart field file and suppresses global errors', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: { protocol: 'mqtt' } });
    const file = new File(['synthetic'], 'protocol.xlsx');
    await importProtocolWorkbook(file);

    const [url, body, config] = vi.mocked(apiClient.post).mock.calls[0]!;
    expect(url).toBe('/config/protocol-excel/import/');
    expect((body as FormData).get('file')).toBe(file);
    expect(config).toMatchObject({
      silent: true,
      headers: { 'Content-Type': undefined },
    });
  });
});
