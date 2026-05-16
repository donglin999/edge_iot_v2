import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite config.
 *
 * The `/api` + `/ws` proxy is a DEV-ONLY concern: `server.proxy` is used by
 * the Vite dev server (`vite serve`) and is never bundled into the production
 * `dist/` build. It is therefore gated behind `command === 'serve'` so it is
 * impossible for the proxy to leak into a production build.
 *
 * Offline / production deployment: the static `dist/` bundle is served by
 * nginx, which must reverse-proxy the SAME two path prefixes to Django:
 *
 *   location /api/ { proxy_pass http://django:8000; }
 *   location /ws/  { proxy_pass http://django:8000;
 *                    proxy_http_version 1.1;
 *                    proxy_set_header Upgrade $http_upgrade;
 *                    proxy_set_header Connection "upgrade"; }
 *
 * See `deploy/nginx.conf.example` for the full reference config. The frontend
 * always calls relative `/api` and `/ws` URLs, so dev (Vite proxy) and prod
 * (nginx) stay aligned with no code changes.
 *
 * The dev proxy target is overridable via `VITE_PROXY_TARGET` (defaults to the
 * `django` docker-compose service).
 */
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxyTarget = env.VITE_PROXY_TARGET || 'http://django:8000';

  return {
    plugins: [react()],
    build: {
      outDir: 'dist',
    },
    // Dev-server-only — excluded entirely from the production build.
    ...(command === 'serve'
      ? {
          server: {
            port: 5173,
            proxy: {
              '/api': {
                target: proxyTarget,
                changeOrigin: true,
              },
              '/ws': {
                target: proxyTarget,
                ws: true,
                changeOrigin: true,
              },
            },
          },
        }
      : {}),
  };
});
