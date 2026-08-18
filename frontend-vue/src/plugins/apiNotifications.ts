import { message } from 'ant-design-vue';

import type { ApiErrorNotifier } from '@/services/apiClient';

export type ShowErrorMessage = (text: string) => unknown;

/** Bridge the framework-neutral API client to Ant Design Vue's supported API. */
export function createApiErrorNotifier(
  showError: ShowErrorMessage = (text) => message.error(text),
): ApiErrorNotifier {
  return (text) => {
    void showError(text);
  };
}
