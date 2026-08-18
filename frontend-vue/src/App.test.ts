import { render, screen } from '@testing-library/vue';
import { createMemoryHistory } from 'vue-router';

import App from './App.vue';
import { antDesignPlugin } from './plugins/antDesign';
import { createAppRouter } from './router';

describe('Vue parallel application shell', () => {
  it('renders the dashboard through the real router and layout', async () => {
    const router = createAppRouter(createMemoryHistory());
    await router.push('/');
    await router.isReady();

    render(App, {
      global: {
        plugins: [antDesignPlugin, router],
      },
    });

    expect(await screen.findByText('系统概览')).toBeInTheDocument();
    expect(screen.getByText('边缘采集控制台')).toBeInTheDocument();
    expect(screen.getByText('Vue 3 平行版本 · Django API')).toBeInTheDocument();
  });
});
