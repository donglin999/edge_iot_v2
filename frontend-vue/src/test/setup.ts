import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/vue';
import { afterEach, vi } from 'vitest';

Object.defineProperty(window, 'scrollTo', {
  configurable: true,
  value: vi.fn(),
  writable: true,
});

Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    addEventListener: vi.fn(),
    addListener: vi.fn(),
    dispatchEvent: vi.fn(),
    matches: false,
    media: query,
    onchange: null,
    removeEventListener: vi.fn(),
    removeListener: vi.fn(),
  })),
  writable: true,
});

const getComputedStyle = window.getComputedStyle.bind(window);
Object.defineProperty(window, 'getComputedStyle', {
  configurable: true,
  value: (element: Element) => getComputedStyle(element),
  writable: true,
});

afterEach(() => cleanup());
