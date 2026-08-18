import axios, {
  AxiosError,
  AxiosHeaders,
  CanceledError,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';

import {
  buildWebSocketUrl,
  formatApiError,
  reportApiError,
  setApiErrorNotifier,
} from './apiClient';

function errorWithResponse(
  status: number,
  data: unknown,
  config: AxiosRequestConfig = { url: '/devices/' },
): AxiosError {
  const internalConfig = {
    ...config,
    headers: new AxiosHeaders(),
  } as InternalAxiosRequestConfig;
  const response = {
    status,
    data,
    statusText: 'synthetic',
    headers: {},
    config: internalConfig,
  } as AxiosResponse;
  return new AxiosError('request failed', 'ERR_SYNTHETIC', internalConfig, undefined, response);
}

afterEach(() => setApiErrorNotifier(null));

describe('API error formatting and notification', () => {
  it('preserves the existing network, server and client-friendly messages', () => {
    const config = { url: '/sites/', headers: new AxiosHeaders() } as InternalAxiosRequestConfig;
    expect(formatApiError(new AxiosError('offline', 'ERR_NETWORK', config))).toBe(
      '网络无响应:/sites/',
    );
    expect(formatApiError(errorWithResponse(503, { detail: '暂不可用' }))).toBe(
      '服务端错误 503: 暂不可用',
    );
    expect(formatApiError(errorWithResponse(400, { message: '参数错误' }))).toBe(
      '请求失败 400: 参数错误',
    );
  });

  it('uses an injected notifier and honors silent requests', () => {
    const notify = vi.fn();
    setApiErrorNotifier(notify);
    const visible = errorWithResponse(500, { detail: 'boom' });
    expect(reportApiError(visible)).toBe(visible);
    expect(notify).toHaveBeenCalledWith('服务端错误 500: boom');

    notify.mockClear();
    reportApiError(errorWithResponse(500, {}, { url: '/probe/', silent: true } as AxiosRequestConfig));
    expect(notify).not.toHaveBeenCalled();
  });

  it('does not report cancellation as an application error', () => {
    const notify = vi.fn();
    setApiErrorNotifier(notify);
    const cancellation = new CanceledError('cancelled');
    expect(axios.isCancel(cancellation)).toBe(true);
    reportApiError(cancellation);
    expect(notify).not.toHaveBeenCalled();
  });
});

describe('buildWebSocketUrl', () => {
  it('uses the current origin and normalizes the leading slash', () => {
    expect(buildWebSocketUrl('ws/acquisition/global/')).toBe(
      `ws://${window.location.host}/ws/acquisition/global/`,
    );
    expect(buildWebSocketUrl('/ws/acquisition/global/')).toBe(
      `ws://${window.location.host}/ws/acquisition/global/`,
    );
  });
});
