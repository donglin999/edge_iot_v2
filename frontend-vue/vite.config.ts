import { fileURLToPath, URL } from 'node:url';

import vue from '@vitejs/plugin-vue';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxyTarget = env.VITE_PROXY_TARGET || 'http://django:8000';

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    build: {
      outDir: 'dist',
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (
              id.includes('/node_modules/ant-design-vue/') ||
              id.includes('/node_modules/@ant-design/icons-vue/')
            ) {
              return 'ant-design-vue';
            }
            return undefined;
          },
        },
      },
    },
    ...(command === 'serve'
      ? {
          server: {
            // React remains on 5173 until the Vue acceptance gate switches traffic.
            port: 5174,
            strictPort: true,
            proxy: {
              '/api': {
                target: proxyTarget,
                changeOrigin: true,
              },
              '/ws': {
                target: proxyTarget,
                changeOrigin: true,
                ws: true,
              },
            },
          },
        }
      : {}),
  };
});
