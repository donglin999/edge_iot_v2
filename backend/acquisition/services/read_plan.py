"""Protocol-agnostic read plan abstraction.

The pipeline builds a :class:`ReadGroup` list **once** when an acquisition task
starts (via :class:`ReadPlanBuilder`), and then on every cycle just calls
``protocol.read_batch(group)`` for each group. The protocol layer is
responsible for performing the actual I/O and decoding the raw words into
engineering values.

The three dataclasses (:class:`PointMeta`, :class:`ReadGroup`,
:class:`Reading`) form the *only* contract the pipeline depends on — adding a
new protocol is then "implement ``read_batch`` and (optionally) a custom plan
builder branch in :func:`ReadPlanBuilder.build`".

This module is deliberately free of Django imports at module scope so it can
be imported from anywhere (Celery worker, management command, unit test).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointMeta:
    """Immutable description of a single point inside a read plan.

    Attributes:
        code: Point code (matches ``Point.code`` in configuration).
        address: 0-based wire-level address (already parsed from SCADA-style /
            4xxxx prefixes).
        num_registers: How many 16-bit Modbus words this point occupies. ``1``
            for ``int16/uint16/bool``, ``2`` for ``int32/uint32/float32``,
            ``4`` for ``int64/uint64/float64``.
        data_type: Decoder hint, lower-case (``"int16"``, ``"float32"``, ...).
        function_code: Modbus FC (``1/2/3/4``); ``None`` for non-Modbus.
        extra: Protocol-private metadata (MQTT topic, OPC-UA node-id, ...).
    """

    code: str
    address: int
    num_registers: int
    data_type: str
    function_code: Optional[int] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ReadGroup:
    """A batch of points the protocol can read in a single I/O call.

    For Modbus this maps onto one ``execute()`` call; for other protocols it
    may degenerate to "one point per group" (the default plan builder).
    """

    protocol_type: str
    points: List[PointMeta]

    # Modbus-private (None for other protocols):
    function_code: Optional[int] = None
    start_address: Optional[int] = None
    register_count: Optional[int] = None

    extra: dict = field(default_factory=dict)


@dataclass
class Reading:
    """A single decoded sample produced by ``protocol.read_batch``.

    This is the *only* product shape the pipeline / sinks consume.
    """

    point_code: str
    value: Any
    timestamp_ns: int
    quality: str  # 'good' | 'bad'
    raw: Any = None


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


class ReadPlanBuilder:
    """Protocol-agnostic entry point for constructing :class:`ReadGroup` lists."""

    @staticmethod
    def build(
        device,
        points: Iterable[Any],
        *,
        gap_threshold: int = 4,
        max_registers: int = 100,
    ) -> List[ReadGroup]:
        """Compress ``points`` into the minimum number of read groups.

        Args:
            device: An object with at least ``protocol`` and ``metadata``
                attributes (typically a ``configuration.models.Device`` ORM
                instance, but any duck-typed stand-in works for tests).
            points: Iterable of ORM ``Point``-like objects exposing ``.code``,
                ``.address``, ``.extra`` and (optional) ``.template``.
            gap_threshold: Maximum address gap (in registers) that may be
                bridged by reading dummy registers in between. ``0`` means
                only strictly contiguous points merge.
            max_registers: Hard cap on the number of registers a single group
                may span (Modbus protocol limit is 125; we default to 100 to
                leave headroom).
        """
        proto = (getattr(device, "protocol", "") or "").lower()
        points_list = list(points)
        if not points_list:
            return []
        if proto.startswith("modbus"):
            return _build_modbus_plan(
                device,
                points_list,
                gap_threshold=gap_threshold,
                max_registers=max_registers,
            )
        return _build_default_plan(device, points_list)


# ---------------------------------------------------------------------------
# Modbus plan
# ---------------------------------------------------------------------------


# Mapping of common data-type aliases -> register count. Mirrors the decoder
# in ``protocols/modbus.py:_combine_registers`` so the plan never asks for a
# different number of words than the decoder will consume.
_DATA_TYPE_REGISTERS = {
    "bool": 1,
    "int16": 1, "uint16": 1, "word": 1, "short": 1,
    "int32": 2, "uint32": 2, "long": 2, "dint": 2, "udint": 2, "dword": 2,
    "float": 2, "float32": 2, "real": 2,
    "int64": 4, "uint64": 4, "lint": 4, "ulint": 4, "qword": 4,
    "float64": 4, "double": 4,
}


def _registers_for(data_type: str, override: Optional[int] = None) -> int:
    """Decide how many registers a point of ``data_type`` occupies.

    An explicit ``num`` override (e.g. supplied via ``point.extra["num"]``)
    always wins; otherwise we look it up by ``data_type``; and finally fall
    back to ``1``.
    """
    if override is not None:
        try:
            n = int(override)
            if n >= 1:
                return n
        except (TypeError, ValueError):
            pass
    return _DATA_TYPE_REGISTERS.get((data_type or "").strip().lower(), 1)


def _parse_modbus_address(addr_str: str) -> int:
    """Mirror ``protocols/modbus.py:_ModbusBase._parse_address``.

    Duplicated here (rather than imported) to keep the read-plan module free
    of any Modbus / modbus-tk import side effects.
    """
    s = str(addr_str or "0").strip()
    prefix = re.match(r"^([DICT])(\d+)$", s, re.IGNORECASE)
    if prefix:
        return int(prefix.group(2))
    try:
        raw = int(s)
    except ValueError:
        return 0
    if raw >= 40001:
        return raw - 40001
    if raw >= 30001:
        return raw - 30001
    if raw >= 10001:
        return raw - 10001
    return raw


def _resolve_data_type(point) -> str:
    """Pick the most specific data-type hint available on a Point.

    Priority: ``point.extra["data_type"]`` > ``point.template.data_type`` >
    ``point.extra["type"]`` (legacy) > ``"uint16"``.
    """
    extra = getattr(point, "extra", None) or {}
    if isinstance(extra.get("data_type"), str) and extra["data_type"].strip():
        return extra["data_type"].strip().lower()
    template = getattr(point, "template", None)
    if template is not None:
        dt = getattr(template, "data_type", "") or ""
        if isinstance(dt, str) and dt.strip():
            return dt.strip().lower()
    legacy = extra.get("type")
    if isinstance(legacy, str) and not legacy.strip().isdigit():
        return legacy.strip().lower()
    return "uint16"


def _build_modbus_plan(
    device,
    points: List[Any],
    *,
    gap_threshold: int,
    max_registers: int,
) -> List[ReadGroup]:
    """Tolerant register coalescing for Modbus-family devices.

    1. Each point is normalised to a ``PointMeta``.
    2. Points are bucketed by ``function_code`` (FC1..FC4 cannot be merged —
       they target different memory areas).
    3. Within a bucket, points are sorted by address and greedily merged when:
         * the gap from the running group's end to the next point's start is
           ``<= gap_threshold`` registers, AND
         * the resulting register span stays ``<= max_registers``.
    4. ``device.metadata['slave_id']`` is read once and stored on
       ``group.extra`` for the protocol layer's convenience — but groups are
       *not* bucketed by slave because a single ``Device`` always represents
       a single Modbus slave.
    """
    proto_type = (getattr(device, "protocol", "") or "modbus_tcp").lower()
    metadata = getattr(device, "metadata", None) or {}
    slave_id = int(metadata.get("slave_id", metadata.get("source_slave_addr", 1)) or 1)

    # 1) normalise + bucket by function code
    by_fc: dict[int, List[PointMeta]] = {}
    for point in points:
        extra = getattr(point, "extra", None) or {}
        fc_raw = extra.get("function_code", extra.get("type", 3))
        try:
            fc = int(fc_raw)
        except (TypeError, ValueError):
            fc = 3
        if fc not in (1, 2, 3, 4):
            fc = 3

        data_type = _resolve_data_type(point)
        num = _registers_for(data_type, extra.get("num"))
        addr = _parse_modbus_address(getattr(point, "address", "0"))

        meta = PointMeta(
            code=getattr(point, "code"),
            address=addr,
            num_registers=num,
            data_type=data_type,
            function_code=fc,
            extra=dict(extra),
        )
        by_fc.setdefault(fc, []).append(meta)

    # 2) greedy coalesce within each fc bucket
    groups: List[ReadGroup] = []
    for fc, metas in by_fc.items():
        metas.sort(key=lambda m: m.address)
        current: List[PointMeta] = []
        cur_start: Optional[int] = None
        cur_end: Optional[int] = None  # exclusive: address of next free reg

        def flush() -> None:
            # Never emit a degenerate group: no points, or a non-positive
            # register span. The old ``else 0`` fallback for a ``None``
            # cur_start would append a 0-register group that read_batch then
            # turns into a bogus ``execute(quantity_of_x=0)`` call.
            if not current or cur_start is None or cur_end is None:
                return
            register_count = cur_end - cur_start
            if register_count <= 0:
                return
            groups.append(
                ReadGroup(
                    protocol_type=proto_type,
                    points=list(current),
                    function_code=fc,
                    start_address=cur_start,
                    register_count=register_count,
                    extra={"slave_id": slave_id},
                )
            )

        for meta in metas:
            point_end = meta.address + meta.num_registers
            if not current:
                current = [meta]
                cur_start = meta.address
                cur_end = point_end
                continue

            # Skip exact duplicates (same address + same width) — typically a
            # point declared twice in the spreadsheet. Keep the first.
            if meta.address == current[-1].address and meta.num_registers == current[-1].num_registers:
                continue

            # Overlap: this point starts *inside* the register range the
            # current group already covers. Because ``metas`` is sorted
            # ascending we also know ``meta.address >= cur_start``, so the
            # point is fully addressable from within the current group.
            # Always fold it in here — feeding a negative ``gap`` into the
            # merge arithmetic below could otherwise flush and open a second
            # group that overlaps the first (redundant reads, and the shared
            # point read twice).
            if meta.address < cur_end:
                current.append(meta)
                cur_end = max(cur_end, point_end)
                continue

            gap = meta.address - cur_end  # non-negative: overlaps handled above
            new_end = max(cur_end, point_end)
            new_span = new_end - cur_start

            mergeable = (
                gap <= gap_threshold
                and new_span <= max_registers
            )
            if mergeable:
                current.append(meta)
                cur_end = new_end
            else:
                flush()
                current = [meta]
                cur_start = meta.address
                cur_end = point_end

        flush()
        # reset per-bucket loop locals
        current = []
        cur_start = None
        cur_end = None

    # Stable order: by (function_code, start_address)
    groups.sort(key=lambda g: (g.function_code or 0, g.start_address or 0))
    return groups


# ---------------------------------------------------------------------------
# Default (one-point-per-group) plan
# ---------------------------------------------------------------------------


# 队列型推协议:read_points 的实现是「把共享接收队列整个抽干,再按传入的测点
# 集合匹配」。这类协议**必须整设备一个读组**——按点拆组的话,第一组的 drain 会
# 把队列清空,但只保留自己那一个测点的消息,其余测点的数据全被扔掉;后续各组
# 再各自空等 read_timeout。中山现场 510 测点实测:MQTT 推送速率远高于入库
# (0.4 点/秒),丢的就是这里。
_QUEUE_DRAIN_PROTOCOLS = {"mqtt", "scada"}


def _build_default_plan(device, points: List[Any]) -> List[ReadGroup]:
    """Fallback for protocols with no native batching (MQTT, OPC-UA v1, ...).

    Each point becomes its own group — except queue-drain protocols (see
    ``_QUEUE_DRAIN_PROTOCOLS``), which get one group holding every point.
    ``function_code`` / ``start_address`` / ``register_count`` are left
    ``None`` because they are Modbus-specific.
    """
    proto_type = (getattr(device, "protocol", "") or "unknown").lower()
    out: List[ReadGroup] = []
    for point in points:
        extra = getattr(point, "extra", None) or {}
        data_type = _resolve_data_type(point)
        num = _registers_for(data_type, extra.get("num"))
        raw_address = getattr(point, "address", 0)
        try:
            addr = int(raw_address)
        except (TypeError, ValueError):
            addr = 0

        # ``PointMeta.address`` 是 Modbus 语义的整数线址,这些协议根本没有。
        # 它们的地址是字符串 —— S7 的 ``DB1.DBD0``、OPC-UA 的 ``ns=2;s=Tag1`` ——
        # 被上面的 int() 打成 0 之后就彻底丢了:S7 每个测点报「无法解析地址」,
        # OPC-UA 去读一个叫 "0" 的节点。原样留一份到 extra 里,
        # ``BaseProtocol.read_batch`` 组 point dict 时会优先用它。
        point_extra = dict(extra)
        if raw_address not in (None, "") and "address" not in point_extra:
            point_extra["address"] = raw_address

        meta = PointMeta(
            code=getattr(point, "code"),
            address=addr,
            num_registers=num,
            data_type=data_type,
            function_code=None,
            extra=point_extra,
        )
        out.append(
            ReadGroup(
                protocol_type=proto_type,
                points=[meta],
                extra={},
            )
        )

    if proto_type in _QUEUE_DRAIN_PROTOCOLS and len(out) > 1:
        merged = [m for g in out for m in g.points]
        return [ReadGroup(protocol_type=proto_type, points=merged, extra={})]
    return out
