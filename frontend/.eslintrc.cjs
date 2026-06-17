/* ESLint 配置 —— React 18 + TypeScript（ESLint 8 经典 .eslintrc 格式） */
module.exports = {
  root: true,
  env: { browser: true, es2020: true, node: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  // 不检查构建产物、E2E/脚本等非 src 源码
  ignorePatterns: [
    'dist',
    'build',
    'node_modules',
    '.eslintrc.cjs',
    'vite.config.ts',
    'e2e',
    'playwright-report',
    '*.mjs',
  ],
  parser: '@typescript-eslint/parser',
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module', ecmaFeatures: { jsx: true } },
  rules: {},
}
