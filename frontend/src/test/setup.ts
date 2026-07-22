/**
 * 测试环境垫片。
 *
 * jsdom 没实现 antd 依赖的几个浏览器 API（matchMedia / ResizeObserver /
 * getComputedStyle 的动画部分），不补的话一渲染 Modal/Select 就炸，
 * 报的还是和被测逻辑毫无关系的错。
 */
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {
      /* 布局观察在 jsdom 里没有意义,存在即可 */
    }
    unobserve() {
      /* 同上 */
    }
    disconnect() {
      /* 同上 */
    }
  };
}

// antd 的 Table/Select 在 jsdom 下会因为拿不到布局信息刷屏 warning，
// 与被测行为无关，这里静音掉，免得真正的报错被淹没。
const IGNORED = [
  'Warning: [antd:',
  '`destroyOnClose` is deprecated',
  'Warning: An update to',
  'not wrapped in act',
  'There may be circular references',
  // jsdom 没实现带伪元素的 getComputedStyle,antd 的动画会踩到,与被测行为无关
  'Not implemented: window.getComputedStyle',
];
const originalError = console.error;
console.error = (...args: unknown[]) => {
  const first = String(args[0] ?? '');
  if (IGNORED.some((pattern) => first.includes(pattern))) return;
  originalError(...args);
};

afterEach(() => {
  cleanup();
});
