/**
 * Unified API client.
 *
 * One axios instance for the whole app, with:
 *  - JSON content negotiation
 *  - 15s default timeout
 *  - Response interceptor that surfaces backend errors via AntD message
 *  - Helper to download a binary blob (Excel template etc.)
 */
import axios, { AxiosError, AxiosRequestConfig } from 'axios';
import { message } from 'antd';

export const apiClient = axios.create({
  baseURL: '/api',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    // Suppress noise on cancellations
    if (axios.isCancel(error)) {
      return Promise.reject(error);
    }
    const data = error.response?.data as Record<string, unknown> | string | undefined;
    let detail = '';
    if (typeof data === 'string') {
      detail = data;
    } else if (data && typeof data === 'object') {
      detail = (data.detail as string) || (data.message as string) || JSON.stringify(data);
    }
    const status = error.response?.status;
    const url = error.config?.url ?? '';
    const friendly =
      status === undefined
        ? `网络无响应:${url}`
        : status >= 500
        ? `服务端错误 ${status}: ${detail || error.message}`
        : `请求失败 ${status}: ${detail || error.message}`;
    // Don't spam if this is a silent probe (caller opted out by setting `silent` meta).
    if (!(error.config as AxiosRequestConfig & { silent?: boolean })?.silent) {
      message.error(friendly);
    }
    return Promise.reject(error);
  },
);

/** Download an arbitrary binary (e.g., Excel template) and trigger save. */
export async function downloadFile(
  url: string,
  filename: string,
  params?: Record<string, string | number | undefined>,
): Promise<void> {
  const response = await apiClient.get(url, {
    params,
    responseType: 'blob',
  });
  const blob = response.data as Blob;
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(objectUrl);
}

/** Build a websocket URL relative to the page origin. */
export function buildWebSocketUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  return `${protocol}//${window.location.host}${cleanPath}`;
}
