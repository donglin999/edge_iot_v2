/**
 * Vitest 配置（与 vite.config.ts 分开，避免测试配置混进构建配置）。
 *
 * 前端此前一行测试都没有，而最近几个把「保存」按钮彻底堵死的 bug——协议选好了
 * 表单里却是空值、scada 任务编码必填却默认开启——lint 和 build 都发现不了，
 * 只有把组件真正渲染起来点一遍才会暴露。所以这里用 jsdom + testing-library，
 * 测的是「用户点下去会发生什么」，不是内部实现细节。
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  // 不挂 @vitejs/plugin-react:它的 fast-refresh preamble 在 jsdom 里没有宿主
  // 页面可插,会直接报 "can't detect preamble"。JSX 交给 esbuild 按 tsconfig 的
  // "jsx": "react-jsx" 转译即可,测试不需要热更新。
  esbuild: { jsx: 'automatic' },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // e2e/ 是 Playwright 的地盘，别让 vitest 去捡
    exclude: ['node_modules', 'dist', 'e2e'],
    restoreMocks: true,
  },
});
