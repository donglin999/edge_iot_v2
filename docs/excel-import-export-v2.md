# 协议配置 Excel 导入导出 v2 —— 每协议两表(设计契约)

## 为什么重设计

现状是「通用 40 列大宽表」:所有协议的字段并成一张表,每行一个测点。
问题(现场实际反馈:很难应用):

1. 一个协议只用其中 7~8 列,其余 30+ 列永远空着,操作员对着一堵空墙填表;
2. **连接参数在每一行测点上重复**(broker/IP/凭证/字节序…),改一台设备的 IP
   要改 N 行,漏一行就裂成两台设备;
3. 设备身份和测点配置搅在同一行,导入器只能靠行间一致性推断设备。

scada 的两表设计(网关连接配一次 + 设备/测点行极简)已被现场验证好用。
v2 把这个思想推广为**所有协议的标准格式**。

## 格式:一个工作簿 = 一个协议,三个 sheet

### Sheet 1「设备」—— 每行一台设备
列 = 固定列 + 该协议的 DEVICE_FIELDS(schema 驱动,来自 FieldSpec,不手抄):

| 固定列 | 说明 |
|---|---|
| `device_name` | 工作簿内唯一的设备显示名,「测点」sheet 用它引用设备;为空则回落用身份拼出的 code |
| `site_code` | 可选,默认 `default` |
| `sample_rate_hz` | 可选,默认 1.0 —— 该设备任务的采样频率(一设备一任务) |

后接该协议 DEVICE_FIELDS 的全部列(必填列表头加 `*` 前缀提示;label 用中文,
第二行放英文键名作机器行——**解析以英文键行为准**,中文行仅展示)。

设备身份 = IDENTITY_FIELDS 拼出 `device.code = {protocol}-{id parts}`(与
现有导入器/表单一致,幂等 upsert 靠它)。

### Sheet 2「测点」—— 每行一个测点
| 固定列 | 说明 |
|---|---|
| `device_name` | 引用 Sheet1 的设备(必填;未知引用=行级错误) |
| `code` | 测点编码(设备内唯一) |
| `description` | 中文名 |

后接该协议 POINT_FIELDS 的全部列(同样中文 label 行 + 英文键行)。

### Sheet 3「使用说明」
人读的填表说明 + **元数据键值区**(解析用):
```
protocol: modbus_tcp
format: v2
```
解析优先读元数据;缺失时按「设备 sheet 列签名 ⊇ 某协议 IDENTITY_FIELDS+必填列」
自动识别,识别不出报明确错误。

## 语义

- **导入**:同步端点(不走 ImportJob/celery),整体一个事务,任何行级错误 →
  400 + 逐行 `{row, sheet, column, message}`,**不写任何数据**(沿 scada 契约)。
  幂等 upsert:设备按 code、测点按 (device, code);merge 语义,不删除表外已有测点。
- **任务**:一设备一任务(既定约束),`task-{device.code}` 幂等 upsert,
  频率取该行 `sample_rate_hz`,绑定该设备本次表内全部测点。
- **导出**:与模板同布局,当前库中该协议设备/测点反拼;**圆环**:导出文件不改
  一字直接导回 = 无变化(0 created)。数值保真:整型列不得出现 `.0`。
- **scada 排除**:它的连接参数在网关实体上,已有专属两表流程,v2 引擎不覆盖;
  模板/导出入口在 UI 里指向既有 scada 流程。闭环行为与 v2 对齐:「网关服务」
  sheet 带任务三列(`task_code`/`task_name`/`sample_rate_hz`),导出自动填
  (从现有任务反推 base 编码,频率取最大),导入时表单字段优先、表内值兜底 ——
  导出→全删→导回,任务同样自动恢复。
- **simulator 排除**(非生产协议)。
- **legacy 兼容**:旧 40 列格式的 ImportJob 流程与解析器**原样保留**(存量文件
  还要能导);新 UI 主路径全部走 v2。40 列模板/导出仍可用但标注 legacy。

## 端点

```
GET  /api/config/protocol-excel/template/?protocol=modbus_tcp   → v2 模板
GET  /api/config/protocol-excel/export/?protocol=modbus_tcp     → 当前配置导出
POST /api/config/protocol-excel/import/   (multipart file)      → 同步导入
     响应: {protocol, created:{devices,points,tasks}, updated:{...},
            devices:[{code, points:[...] , task}], errors:[]}
     行级错误: 400 + {errors:[{sheet,row,column,message}]}
```

## 前端

- 设备管理页「下载 Excel 模板」下拉重组:
  - 每个生产协议一项(动态自协议清单):「modbus_tcp 模板(设备+测点两表)」
  - scada 项 → 既有网关两表模板
  - 「通用单表模板(legacy)」沉底
- 同位置新增「导入配置」按钮:上传 → v2 自动识别协议 → 成功 toast+刷新 /
  失败逐行错误列表(样式沿 scada 导入的错误呈现)。旧 40 列文件提示走「导入作业」页。
- 「导出当前设备」:选中具体协议筛选时走 v2 per-protocol 导出;「全部」时仍走
  40 列全量导出(legacy,跨协议只有大宽表装得下)。

## 实现约束

- 列定义、必填、类型、枚举、默认值全部从 FieldSpec 派生,**零手抄**;新增协议
  自动获得模板/导入/导出,不改本模块(与导入器同一设计纪律)。
- 数值解析统一容忍 pandas/openpyxl 浮点(`8883.0`→8883),经由协议 coerce_device/
  coerce_point(已修过的 _coerce_enum 返回 choice 类型)。
- 单元格类型:整型字段写 int、浮点写 float、布尔写 TRUE/FALSE,防止导出再导入
  的类型漂移。
