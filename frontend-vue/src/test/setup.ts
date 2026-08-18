import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/vue';
import { afterEach, vi } from 'vitest';

Object.defineProperty(window, 'scrollTo', {
  configurable: true,
  value: vi.fn(),
  writable: true,
});

afterEach(() => cleanup());
