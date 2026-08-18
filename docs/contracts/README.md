# M1 兼容契约冻结

本目录冻结 `dfabcc2` Django/React 基线的**可观察行为**，作为 Vue 3 与 FastAPI
双跑比对的输入。它不是新 API 设计，也不代表当前行为都合理。

规范文件：

- `rest-v1.json`：62 条业务 REST 规范路径、HTTP 方法、分页、错误家族与数值类型；
- `websocket-v1.json`：2 条 WS 路径、10 种转发帧、Redis 有效期及浏览器重连/回补规则；
- `excel-v1.json`：v2、legacy、SCADA 三套工作簿的 sheet、表头及协议 FieldSpec 快照。

## 兼容判定

迁移实现必须逐项通过 `backend/tests/contracts/`。任何 fixture 变化都视为契约变化，
必须在同一个提交中说明调用方、兼容窗口、回滚方式和新 contract id，不能为了让测试
变绿直接重录快照。新增字段可以兼容旧客户端时仍需记录；删除、重命名、类型变化、
HTTP 状态变化和信封变化一律属于破坏性变更。

JSON fixture 使用合成标识，不包含现场 IP、账号、密码或 token。Excel 快照只记录
字段定义，不提交导出的二进制工作簿。

## 当前最重要的 legacy 行为

- DRF 通用错误、`detail` 错误、v2 Excel 和 SCADA Excel 是四种不同信封；
- Influx 查询失败时 `point-history` 仍可能返回 HTTP 200，并额外带 `error`；
- ORM DecimalField 在 JSON 中是定点字符串，例如 `"2.50"`，不是数值 `2.5`；
- WS 没有序号、游标和重放，断线后必须靠 REST/Influx 回补；
- session/global 对同类数据分别使用 `data_point` / `data_point_update`；
- v2 是双表头，SCADA/legacy 是单表头，三者错误字段也不同；
- 当前 REST 与 WS 都没有应用级鉴权；认证/RBAC 留到 M8 做协调切换；
- 部分 Excel 导出可能携带明文凭证，fixture 仅允许合成值；修复必须设计
  “脱敏显示 + 留空保留原密钥”等显式语义。

`docs/API_REFERENCE.md` 是历史说明，不再作为迁移契约依据；实现、fixture 与可执行测试
三者不一致时，以 fixture + 测试捕获的实际基线为准，并先提交契约变更评审。

## 本地执行

```bash
cd backend
python -m pytest tests/contracts/ -c tests/pytest.ini
```

这些用例不启动 Docker、Redis、InfluxDB 或现场协议模拟器。
