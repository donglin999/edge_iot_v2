# Vue 3 平行前端

这是迁移期间与现有 `frontend/` React 应用并行运行的 Vue 3 入口。M2/M3 验收完成前，
它不会替换 Docker Compose、Nginx 或离线包中的默认前端。

## 本地门禁

要求 Node.js `20.19+`、`22.12+` 或更高受支持版本：

```bash
npm ci
npm run typecheck
npm test
npm run build
```

开发服务器默认使用 `5174`，避免与 React 的 `5173` 冲突。`/api` 和 `/ws` 始终使用
相对地址；开发模式通过 `VITE_PROXY_TARGET`（默认 `http://django:8000`）转发，生产构建
仍由 Nginx 提供同路径反向代理。

当前只交付应用壳、8 条兼容路由、主题、错误边界和测试基线。所有写请求在迁移期仍由
Django 单独处理。
