\n## 2025-10-06 导入流程验证记录\n- 新增 ImportJob API 支持 ile_path 与 site_code 参数，可触发 process_excel 校验并写入配置库。\n- 导入落库逻辑：幂等更新 Device/Point，空值统一清理，并生成 ConfigVersion 快照。\n- 单元测试覆盖校验、apply、重复导入场景（configuration/tests/test_import_workflow.py）。

- 2025-10-06：导入页面改为上传文件（拖拽/点击），后端改用 `file` 字段并保存到 uploads/import_jobs/，`/apply/` 接口默认写入 default 站点。

- 2025-10-06：新增 `/api/config/import-jobs/{id}/diff/` 用于导入前后差异比对；前端 API 请求统一附带 `site_code=default`。

- 2025-10-06：实现任务启停 API（start/stop），前端控制按钮及导入差异视图上线。
