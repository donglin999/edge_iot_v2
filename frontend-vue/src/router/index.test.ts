import { createMemoryHistory } from 'vue-router';

import { APP_ROUTES, createAppRouter } from './index';

describe('Vue application routes', () => {
  it('preserves all eight React application URLs', () => {
    const root = APP_ROUTES.find((route) => route.path === '/');
    const childPaths = root?.children?.map((route) => route.path);

    expect(childPaths).toEqual([
      '',
      'acquisition',
      'devices',
      'devices/:id',
      'data',
      'import',
      'versions',
      'alarms',
    ]);
  });

  it('resolves a dynamic device detail route without changing its URL contract', async () => {
    const router = createAppRouter(createMemoryHistory());
    await router.push('/devices/42');

    expect(router.currentRoute.value.name).toBe('device-detail');
    expect(router.currentRoute.value.params.id).toBe('42');
  });
});
