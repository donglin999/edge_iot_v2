import { describe, expect, it, vi } from 'vitest';

import { createApiErrorNotifier } from './apiNotifications';

describe('createApiErrorNotifier', () => {
  it('uses the explicit Ant Design message bridge without globalProperties', () => {
    const showError = vi.fn();
    const notify = createApiErrorNotifier(showError);

    notify('请求失败 500: boom');

    expect(showError).toHaveBeenCalledWith('请求失败 500: boom');
  });
});
