import axios, { type AxiosError, type AxiosRequestConfig } from 'axios';

export type ApiErrorNotifier = (message: string) => void;

type SilentRequestConfig = AxiosRequestConfig & { silent?: boolean };

let errorNotifier: ApiErrorNotifier | null = null;

export function setApiErrorNotifier(notifier: ApiErrorNotifier | null): void {
  errorNotifier = notifier;
}

export const apiClient = axios.create({
  baseURL: '/api',
  timeout: 15_000,
  headers: { 'Content-Type': 'application/json' },
});

function responseDetail(data: unknown): string {
  if (typeof data === 'string') return data;
  if (!data || typeof data !== 'object') return '';
  const record = data as Record<string, unknown>;
  if (typeof record.detail === 'string') return record.detail;
  if (typeof record.message === 'string') return record.message;
  try {
    return JSON.stringify(record);
  } catch {
    return '';
  }
}

export function formatApiError(error: AxiosError): string {
  const detail = responseDetail(error.response?.data);
  const status = error.response?.status;
  const url = error.config?.url ?? '';
  if (status === undefined) return `网络无响应:${url}`;
  if (status >= 500) return `服务端错误 ${status}: ${detail || error.message}`;
  return `请求失败 ${status}: ${detail || error.message}`;
}

export function reportApiError(error: AxiosError): AxiosError {
  if (!axios.isCancel(error) && !(error.config as SilentRequestConfig | undefined)?.silent) {
    errorNotifier?.(formatApiError(error));
  }
  return error;
}

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => Promise.reject(reportApiError(error)),
);

export async function downloadFile(
  url: string,
  filename: string,
  params?: Record<string, string | number | undefined>,
): Promise<void> {
  const response = await apiClient.get(url, { params, responseType: 'blob' });
  const objectUrl = URL.createObjectURL(response.data as Blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(objectUrl);
}

export function buildWebSocketUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  return `${protocol}//${window.location.host}${cleanPath}`;
}
