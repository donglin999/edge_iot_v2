"""Two-sheet SCADA Excel: template generation, export, and import parsing.

The generic multi-protocol template (``acquisition.services.templates``) makes
every row repeat the whole MQTT connection block. For SCADA that block is
identical across the entire fleet — only ``device_name`` and the point ``code``
actually vary — so this module splits the file in two:

* 「网关服务」 — exactly one row: the shared MQTT connection config, i.e. the
  :class:`configuration.models.ScadaGateway` fields.
* 「设备与测点」 — one row per *point*; rows sharing a ``device_name`` collapse
  into one device.

Parsing produces the very same payload shape the ``provision`` API takes, so
Excel import and the UI both land in
:func:`configuration.services.scada_provision.provision` — one code path, one
set of idempotency/transaction guarantees.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .importer import RowError
from .. import models

#: Protocol tag written into ``RowError.protocol`` and ``Point.extra``.
SCADA_PROTOCOL = "scada"

SHEET_GATEWAY = "网关服务"
SHEET_POINTS = "设备与测点"
SHEET_NOTES = "使用说明"

#: Mirrors ``acquisition.protocols.scada`` POINT_FIELDS' data_type enum.
DATA_TYPES = ("string", "int", "float", "bool")

#: Never ship a real secret in a downloadable template.
PASSWORD_PLACEHOLDER = "<填写真实密码>"

# Styling mirrors acquisition/services/templates.py so the two downloads look
# like they came from the same product.
_HEADER_FILL = PatternFill("solid", fgColor="FFE3F2FD")
_HEADER_FONT = Font(bold=True)
_REQUIRED_FILL = PatternFill("solid", fgColor="FFFFF59D")


@dataclass(frozen=True)
class ColumnSpec:
    """One Excel column: the English header key plus its authoring help."""

    name: str
    label: str
    required: bool = False
    help_text: str = ""
    choices: Sequence[str] = ()
    example: Any = None


GATEWAY_COLUMNS: Tuple[ColumnSpec, ...] = (
    ColumnSpec("code", "网关编码", required=True,
               help_text="唯一标识；导入时按此编码更新已有网关（不会重复创建）", example="zs-scada"),
    ColumnSpec("name", "网关名称", required=True, example="中山小家电 SCADA 网关"),
    ColumnSpec("source_ip", "MQTT 地址", required=True,
               help_text="MQTT broker 的 IP 或域名", example="10.134.14.147"),
    ColumnSpec("source_port", "MQTT 端口", help_text="默认 8883（TLS）", example=8883),
    ColumnSpec("mqtt_use_tls", "启用 TLS", choices=("TRUE", "FALSE"),
               help_text="TRUE / FALSE；也接受 1/0、是/否", example=True),
    ColumnSpec("mqtt_username", "用户名", example="ZYY_XJDZS"),
    ColumnSpec("mqtt_password", "密码",
               help_text="模板中为占位符，请替换为真实密码", example=PASSWORD_PLACEHOLDER),
    ColumnSpec("mqtt_qos", "QoS", choices=("0", "1", "2"), example=0),
    ColumnSpec("mqtt_client_id", "客户端 ID", help_text="可留空，留空则由采集端自动生成", example="edge-iot-1"),
    ColumnSpec("mqtt_read_timeout", "读超时(秒)", example=5.0),
    ColumnSpec("product_key", "产品 Key", help_text="话题模板中的 {product_key} 段",
               example="123daffb91264286adcdf3bfe55194c7"),
    ColumnSpec("topic_template", "话题模板",
               help_text="支持 {product_key} {device_name} {code} 占位符",
               example=models.ScadaGateway.DEFAULT_TOPIC_TEMPLATE),
)

POINT_COLUMNS: Tuple[ColumnSpec, ...] = (
    ColumnSpec("device_name", "设备名", required=True,
               help_text="话题中的 {device_name} 段；相同 device_name 的多行自动归为同一台设备",
               example="A0201010001150403"),
    ColumnSpec("device_label", "设备中文名", help_text="可留空，留空时取 device_name", example="注塑机1"),
    ColumnSpec("code", "测点编码", required=True,
               help_text="话题中的 {code} 段", example="N270400150027"),
    ColumnSpec("description", "中文名称", example="注射压力实际值"),
    ColumnSpec("data_type", "数据类型", choices=DATA_TYPES,
               help_text="可选值：string / int / float / bool；默认 float", example="float"),
    ColumnSpec("unit", "单位", example="MPa"),
)

_GATEWAY_EXAMPLE_ROW = {c.name: c.example for c in GATEWAY_COLUMNS}
_POINT_EXAMPLE_ROWS = (
    {
        "device_name": "A0201010001150403", "device_label": "注塑机1",
        "code": "N270400150027", "description": "注射压力实际值",
        "data_type": "float", "unit": "MPa",
    },
    {
        "device_name": "A0201010001150403", "device_label": "注塑机1",
        "code": "N270400150028", "description": "锁模力实际值",
        "data_type": "float", "unit": "kN",
    },
)

_NOTES_ROWS = (
    ["中山小家电 SCADA — 网关配置模板"],
    [""],
    ["1) 「网关服务」sheet 只填一行：整个 SCADA 车间共用同一套 MQTT 连接配置。"],
    ["2) 「设备与测点」sheet 每行一个测点；device_name 相同的多行自动归为同一台设备。"],
    ["3) 不需要在设备行里重复任何 MQTT 字段（地址/端口/TLS/账号/密码/QoS/产品Key/话题模板）。"],
    ["4) 黄底列为必填项；鼠标悬停表头可看中文名 / 取值范围 / 示例。"],
    ["5) 导入：POST /api/config/scada-gateways/import/（multipart，字段名 file）。"],
    ["   可选表单字段 task_code / task_name / sample_rate_hz —— 填了 task_code 就顺带创建采集任务，"],
    ["   并把本次导入的全部测点绑定到该任务；不填则只建设备和测点。"],
    ["6) 导入按 code 幂等：同一份文件导入多次不会产生重复设备/测点，只会就地更新。"],
    ["7) 导出：GET /api/config/scada-gateways/{id}/export/ 得到同格式文件，可改完再导入。"],
    [""],
    ["技术细节："],
    ["* 导入与页面上的「批量创建」走同一个 provision 服务，行为完全一致。"],
    ["* 模板里的密码是占位符 " + PASSWORD_PLACEHOLDER + "，请替换为真实密码后再导入。"],
)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _write_header(ws, columns: Sequence[ColumnSpec]) -> None:
    """Header row: English keys, required cells highlighted, Chinese comments."""
    for col_idx, spec in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_idx, value=spec.name)
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.fill = _REQUIRED_FILL if spec.required else _HEADER_FILL

        lines = [spec.label]
        if spec.required:
            lines.append("【必填】")
        if spec.help_text:
            lines.append(spec.help_text)
        if spec.choices:
            lines.append("可选值: " + ", ".join(map(str, spec.choices)))
        cell.comment = Comment("\n".join(lines), "edge-iot")

        ws.column_dimensions[get_column_letter(col_idx)].width = max(16, len(spec.name) + 6)
    ws.freeze_panes = "A2"


def _write_rows(ws, columns: Sequence[ColumnSpec], rows: Sequence[Dict[str, Any]]) -> None:
    for r_offset, row in enumerate(rows, start=2):
        for col_idx, spec in enumerate(columns, start=1):
            ws.cell(row=r_offset, column=col_idx, value=row.get(spec.name))


def _write_notes(wb: Workbook) -> None:
    notes = wb.create_sheet(SHEET_NOTES)
    for row in _NOTES_ROWS:
        notes.append(row)
    notes.column_dimensions["A"].width = 96


def _build(gateway_rows: Sequence[Dict[str, Any]], point_rows: Sequence[Dict[str, Any]]) -> bytes:
    wb = Workbook()
    gw_ws = wb.active
    gw_ws.title = SHEET_GATEWAY
    _write_header(gw_ws, GATEWAY_COLUMNS)
    _write_rows(gw_ws, GATEWAY_COLUMNS, gateway_rows)

    pt_ws = wb.create_sheet(SHEET_POINTS)
    _write_header(pt_ws, POINT_COLUMNS)
    _write_rows(pt_ws, POINT_COLUMNS, point_rows)

    _write_notes(wb)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_scada_template() -> bytes:
    """Blank-but-illustrative two-sheet workbook, ready to hand to an operator."""
    return _build([_GATEWAY_EXAMPLE_ROW], list(_POINT_EXAMPLE_ROWS))


def build_scada_export(gateway: models.ScadaGateway) -> bytes:
    """Serialise a gateway's live config into the *same* two-sheet format.

    The output is import-ready, which is what makes export → edit → import a
    supported workflow rather than a one-way dump.
    """
    gateway_row = {spec.name: getattr(gateway, spec.name) for spec in GATEWAY_COLUMNS}

    point_rows: List[Dict[str, Any]] = []
    devices = gateway.devices.prefetch_related("points").order_by("code")
    for device in devices:
        # The device_name is the only per-device datum the fleet doesn't share;
        # fall back to the code suffix if metadata was hand-edited away.
        device_name = (device.metadata or {}).get("scada_device_name") or device.code
        for point in device.points.order_by("code"):
            extra = point.extra or {}
            point_rows.append({
                "device_name": device_name,
                "device_label": device.name,
                "code": point.code,
                "description": point.description,
                "data_type": extra.get("data_type") or "float",
                "unit": extra.get("unit") or "",
            })

    return _build([gateway_row], point_rows)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedWorkbook:
    """Parse result: either ``errors`` is non-empty, or the payload is usable."""

    gateway: Dict[str, Any]
    devices: List[Dict[str, Any]]
    errors: List[RowError]

    @property
    def is_valid(self) -> bool:
        return not self.errors


_TRUE_VALUES = {"true", "1", "yes", "y", "是", "t"}
_FALSE_VALUES = {"false", "0", "no", "n", "否", "f"}


def _clean(value: Any) -> str:
    """Normalise a cell to a trimmed string; ``None``/blank → ``""``."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        # openpyxl hands back 8883.0 for an integer-looking cell.
        return str(int(value))
    return str(value).strip()


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def _read_sheet(ws) -> Tuple[List[str], List[Tuple[int, Dict[str, Any]]]]:
    """Return (headers, [(excel_row_number, {header: value})]).

    Wholly-empty rows are dropped — Excel loves to hand back a few hundred of
    them below the real data.
    """
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return [], []

    headers = [_clean(h) for h in header_row]
    records: List[Tuple[int, Dict[str, Any]]] = []
    for offset, row in enumerate(rows_iter, start=2):
        if row is None or all(v is None or _clean(v) == "" for v in row):
            continue
        record = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            record[header] = row[idx] if idx < len(row) else None
        records.append((offset, record))
    return headers, records


def _missing_columns(headers: Sequence[str], columns: Sequence[ColumnSpec]) -> List[str]:
    present = set(headers)
    return [c.name for c in columns if c.required and c.name not in present]


def _parse_gateway_sheet(ws) -> Tuple[Dict[str, Any], List[RowError]]:
    errors: List[RowError] = []
    headers, records = _read_sheet(ws)

    missing = _missing_columns(headers, GATEWAY_COLUMNS)
    if missing:
        errors.append(RowError(
            1, ", ".join(missing), f"「{SHEET_GATEWAY}」缺少必要列: {', '.join(missing)}",
            protocol=SCADA_PROTOCOL,
        ))
        return {}, errors

    if not records:
        errors.append(RowError(
            2, "code", f"「{SHEET_GATEWAY}」没有数据行，请填写一行网关配置",
            protocol=SCADA_PROTOCOL,
        ))
        return {}, errors
    if len(records) > 1:
        errors.append(RowError(
            records[1][0], "code",
            f"「{SHEET_GATEWAY}」只能有一行：整个网关共用一套 MQTT 配置",
            protocol=SCADA_PROTOCOL,
        ))
        return {}, errors

    row_num, record = records[0]
    data: Dict[str, Any] = {}

    for spec in GATEWAY_COLUMNS:
        raw = record.get(spec.name)
        text = _clean(raw)
        if spec.required and not text:
            errors.append(RowError(row_num, spec.name, f"{spec.label}不能为空",
                                   protocol=SCADA_PROTOCOL))
            continue
        if not text and spec.name not in ("code", "name", "source_ip"):
            # Blank optional cell → let the model default apply.
            continue
        data[spec.name] = text

    # --- typed fields ---
    if "source_port" in data:
        port = _to_int(data["source_port"])
        if port is None or not (1 <= port <= 65535):
            errors.append(RowError(row_num, "source_port", "端口必须是 1-65535 的整数",
                                   protocol=SCADA_PROTOCOL))
        else:
            data["source_port"] = port

    if "mqtt_use_tls" in data:
        flag = _coerce_bool(record.get("mqtt_use_tls"))
        if flag is None:
            errors.append(RowError(row_num, "mqtt_use_tls", "请填写 TRUE 或 FALSE",
                                   protocol=SCADA_PROTOCOL))
        else:
            data["mqtt_use_tls"] = flag

    if "mqtt_qos" in data:
        qos = _to_int(data["mqtt_qos"])
        if qos not in (0, 1, 2):
            errors.append(RowError(row_num, "mqtt_qos", "QoS 只能是 0 / 1 / 2",
                                   protocol=SCADA_PROTOCOL))
        else:
            data["mqtt_qos"] = qos

    if "mqtt_read_timeout" in data:
        timeout = _to_float(data["mqtt_read_timeout"])
        if timeout is None or timeout <= 0:
            errors.append(RowError(row_num, "mqtt_read_timeout", "读超时必须是正数",
                                   protocol=SCADA_PROTOCOL))
        else:
            data["mqtt_read_timeout"] = timeout

    if data.get("mqtt_password") == PASSWORD_PLACEHOLDER:
        errors.append(RowError(row_num, "mqtt_password",
                               f"请把占位符 {PASSWORD_PLACEHOLDER} 替换为真实密码",
                               protocol=SCADA_PROTOCOL))

    return data, errors


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_points_sheet(ws) -> Tuple[List[Dict[str, Any]], List[RowError]]:
    """Fold one-row-per-point into the provision service's device payloads."""
    errors: List[RowError] = []
    headers, records = _read_sheet(ws)

    missing = _missing_columns(headers, POINT_COLUMNS)
    if missing:
        errors.append(RowError(
            1, ", ".join(missing), f"「{SHEET_POINTS}」缺少必要列: {', '.join(missing)}",
            protocol=SCADA_PROTOCOL,
        ))
        return [], errors

    if not records:
        errors.append(RowError(2, "device_name", f"「{SHEET_POINTS}」没有数据行",
                               protocol=SCADA_PROTOCOL))
        return [], errors

    # Insertion-ordered so the response mirrors the operator's own row order.
    by_device: Dict[str, Dict[str, Any]] = {}
    seen_codes: Dict[Tuple[str, str], int] = {}

    for row_num, record in records:
        device_name = _clean(record.get("device_name"))
        code = _clean(record.get("code"))

        if not device_name:
            errors.append(RowError(row_num, "device_name", "设备名不能为空",
                                   protocol=SCADA_PROTOCOL))
        if not code:
            errors.append(RowError(row_num, "code", "测点编码不能为空",
                                   protocol=SCADA_PROTOCOL))
        if not device_name or not code:
            continue

        data_type = _clean(record.get("data_type")).lower() or "float"
        if data_type not in DATA_TYPES:
            errors.append(RowError(
                row_num, "data_type",
                f"未知数据类型 '{data_type}'，可选值: {', '.join(DATA_TYPES)}",
                protocol=SCADA_PROTOCOL,
            ))
            continue

        key = (device_name, code)
        if key in seen_codes:
            errors.append(RowError(
                row_num, "code",
                f"测点 '{code}' 在设备 '{device_name}' 下重复（第 {seen_codes[key]} 行已出现）",
                protocol=SCADA_PROTOCOL,
            ))
            continue
        seen_codes[key] = row_num

        device = by_device.setdefault(device_name, {
            "device_name": device_name,
            "name": "",
            "points": [],
        })
        # First non-blank label wins — operators typically fill it on the
        # device's first row only.
        if not device["name"]:
            device["name"] = _clean(record.get("device_label"))

        device["points"].append({
            "code": code,
            "description": _clean(record.get("description")),
            "data_type": data_type,
            "unit": _clean(record.get("unit")),
        })

    return list(by_device.values()), errors


def parse_scada_workbook(source) -> ParsedWorkbook:
    """Parse an uploaded two-sheet workbook into a provision payload.

    Args:
        source: Anything openpyxl accepts — an ``UploadedFile``, a path, or a
            ``BytesIO``.

    Returns:
        A :class:`ParsedWorkbook`. Callers must check ``is_valid`` before
        using the payload; parsing never writes to the database, so a failed
        parse leaves nothing behind.
    """
    try:
        wb = load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises a zoo of types for bad files
        return ParsedWorkbook({}, [], [RowError(
            0, "", f"无法读取 Excel 文件: {exc}", protocol=SCADA_PROTOCOL,
        )])

    try:
        missing_sheets = [s for s in (SHEET_GATEWAY, SHEET_POINTS) if s not in wb.sheetnames]
        if missing_sheets:
            return ParsedWorkbook({}, [], [RowError(
                0, "", f"缺少必要的 sheet: {', '.join(missing_sheets)}",
                protocol=SCADA_PROTOCOL,
            )])

        gateway_data, gateway_errors = _parse_gateway_sheet(wb[SHEET_GATEWAY])
        devices, point_errors = _parse_points_sheet(wb[SHEET_POINTS])
    finally:
        wb.close()

    return ParsedWorkbook(gateway_data, devices, gateway_errors + point_errors)


def build_task_payload(
    task_code: str = "",
    task_name: str = "",
    sample_rate_hz: Any = None,
) -> Optional[Dict[str, Any]]:
    """Shape the optional import-time task into the provision payload's ``task``.

    No ``task_code`` → no task, which is the "just load the points" case.
    """
    code = _clean(task_code)
    if not code:
        return None
    payload: Dict[str, Any] = {"code": code, "name": _clean(task_name) or code}
    rate = _to_float(sample_rate_hz) if sample_rate_hz not in (None, "") else None
    if rate is not None and rate > 0:
        payload["sample_rate_hz"] = rate
    return payload
