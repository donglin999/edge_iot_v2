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

// `Warning: [antd:` 曾经用来静音两类噪音:(1) destroyOnClose 弃用告警 ——
// 已经把全仓库的 destroyOnClose 都改成 destroyOnHidden,这条本身消失了;
// (2) message/Modal 静态方法拿不到 ConfigProvider theme 的 "Static function
// can not consume context" 告警 —— 已经把用得到的调用点都改成 App.useApp()
// 拿 context-aware 实例。经验证,当前 50 个用例全跑一遍不再触发任何
// `[antd:` 前缀的告警,这条静音规则整体收掉。
// 仍需要静音的:
const IGNORED = [
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
