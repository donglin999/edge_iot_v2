"""SCPI serial protocol for the LINO 安规测试仪 (electrical-safety / hi-pot tester).

沙永健工厂分支 (XIU-142, 源自 XIU-141 附件 ``SCPI协议.pdf``).

LINO 安规测试仪走 **ASCII 串口指令** (RS-232 / RS-485)。本模块把这套指令集拆成三层,
互不耦合、都能脱离 Django / 硬件单测:

1. **编解码 (codec)** —— :func:`encode`/:func:`decode` 负责结束标记 (NL / ^END),
   :class:`ScpiCommand` + :func:`parse_command` 负责把一行指令拆成 子系统/节点/参数,
   ``build_*`` 系列负责把结构化意图拼成 wire 字符串。这是协议的核心,验收的「解析」部分。
2. **仪器仿真 (simulator)** —— :class:`LinoSafetyTester` 维护测试组/步/参数/状态/结果,
   对任意指令给出符合 PDF 示例格式的应答。这是验收的「应答」部分,也让自测无需真机。
3. **采集适配 (adapter)** —— :class:`SCPIProtocol` 继承 :class:`BaseProtocol`,
   用 pyserial 把指令送到真机 (``serial_port=loopback`` 时回环到内置仿真器),
   把 ``STAT`` / ``RESU`` 之类查询映射成采集测点。

文档约定:指令里的 ``_`` 代表空格 (ASCII 0x20);``\`` 代表「无该项」按 0 写入。
结束标记默认 ``NL`` = 整数 10 (0x0A),可选 ``^END`` (EOI)。

----------------------------------------------------------------------------
⚠ 假设标注 (PDF 不在手,基于 XIU-142 描述实现,待测试工程师对照 PDF 核对):
  * ``FETC:RESU_?`` 单步返回模板 ``STEP_N1:N2_N3_N4_N5_N6`` 取
    N1=步号, N2=测试项目编码, N3..N5=三个测量读数, N6=单步状态(0/1/2/3)。
    N3..N5 各自的物理量 (电压/电流/电阻) 随测试项目类型而定,本实现按位置透传。
  * 多步结果之间用 ``;`` 分隔 (SCPI 惯例),整体末尾加结束标记。
----------------------------------------------------------------------------
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolError,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认结束标记 NL = 整数 10 (0x0A)。
TERMINATOR_NL = b"\x0a"

#: 可选 EOI 结束标记 (附在指令尾、结束符之前)。
EOI_MARKER = "^END"

#: 文档里 ``_`` 代表空格。
DOC_SPACE = "_"

#: 文档里 ``\`` 代表「无该项」,按 0 写入。
NO_ITEM = "\\"

#: PARA 指令固定写入 9 个参数。
PARA_PARAM_COUNT = 9

#: 测试步范围 1-8。
STEP_MIN, STEP_MAX = 1, 8

#: 测试组范围 01-99。
GROUP_MIN, GROUP_MAX = 1, 99

#: 测试项目编码 → 助记符。
ITEM_CODES: Dict[int, str] = {
    0: "EMPTY",   # 空项
    1: "GB",      # 接地
    2: "IR",      # 绝缘
    3: "ACW",     # 交耐
    4: "DCW",     # 直耐
    5: "LVS",     # 低压启动
    6: "POWER",   # 功率
    7: "LEAK_L",  # 动泄
    8: "LEAK_U",  # 静泄
}
ITEM_CODE_MIN, ITEM_CODE_MAX = 0, 8

#: 测试项目编码 → 中文名。
ITEM_LABELS: Dict[int, str] = {
    0: "空项", 1: "接地(GB)", 2: "绝缘(IR)", 3: "交耐(ACW)", 4: "直耐(DCW)",
    5: "低压启动(LVS)", 6: "功率(POWER)", 7: "动泄(LEAK_L)", 8: "静泄(LEAK_U)",
}

#: MMEM:STAT / 单步状态码。
STATUS_READY, STATUS_TESTING, STATUS_OK, STATUS_NG = 0, 1, 2, 3
STATUS_LABELS = {0: "Ready", 1: "Testing", 2: "OK", 3: "NG"}

#: DISP:PAGE 允许的页面。
DISP_PAGES = ("SYST", "FILE", "TEST")

#: 多步结果分隔符 (见文件头假设标注)。
RESULT_SEP = ";"


class ScpiError(ProtocolError):
    """SCPI 指令构造 / 解析错误。"""


# ---------------------------------------------------------------------------
# 编解码:结束标记
# ---------------------------------------------------------------------------


def encode(text: str, *, use_eoi: bool = False, terminator: bytes = TERMINATOR_NL) -> bytes:
    """把一行 ASCII 指令编码成 wire bytes (追加 ^END / 结束标记)。

    Args:
        text: 不含结束标记的指令正文,如 ``"SOUR:STEP 1:3"``。
        use_eoi: True 时在结束标记前追加 ``^END``。
        terminator: 结束标记字节,默认 NL (0x0A)。
    """
    body = text + (EOI_MARKER if use_eoi else "")
    return body.encode("ascii") + terminator


def decode(raw: bytes, *, terminator: bytes = TERMINATOR_NL) -> str:
    """去掉结束标记 / ``^END`` / 周边空白,返回指令正文字符串。"""
    if isinstance(raw, (bytes, bytearray)):
        s = bytes(raw).decode("ascii", errors="replace")
    else:
        s = str(raw)
    term = terminator.decode("ascii", errors="replace")
    if term and s.endswith(term):
        s = s[: -len(term)]
    s = s.strip("\r\n")
    if s.endswith(EOI_MARKER):
        s = s[: -len(EOI_MARKER)]
    return s.strip()


# ---------------------------------------------------------------------------
# 解析:一行指令 → ScpiCommand
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScpiCommand:
    """解析后的 SCPI 指令。

    例: ``PARA:STEP 1:6 220 0 0 0 4 0.6 2100 0.03 3`` →
        subsystem="PARA", node="STEP", args=["1:6", "220", ..., "3"], is_query=False
    """

    subsystem: str            # SAFE / DISP / SOUR / PARA / MMEM / FETC
    node: str                 # STAR / STOP / PAGE / STEP / NAME / SAVE / STAT / GROU / AUTO / RESU
    args: Tuple[str, ...] = ()
    is_query: bool = False
    raw: str = ""

    @property
    def head(self) -> str:
        """``SUBSYS:NODE`` 头。"""
        return f"{self.subsystem}:{self.node}"


def _normalize_separators(text: str) -> str:
    """文档用 ``_`` 表示空格;真机也用空格。统一成单个空格便于解析。

    注意只替换「分隔用」下划线;不把指令拆碎 —— 直接全部替换即可,因为指令集里
    没有任何节点助记符 / 参数本身含下划线 (NAME 的取值在协议里也不含下划线)。
    """
    text = text.replace(DOC_SPACE, " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_command(raw: Any, *, terminator: bytes = TERMINATOR_NL) -> ScpiCommand:
    """把一行指令 (bytes / str,可含结束标记) 解析成 :class:`ScpiCommand`。

    容错:同时接受 ``_`` 与空格作为分隔符。
    """
    if isinstance(raw, (bytes, bytearray)):
        text = decode(raw, terminator=terminator)
    else:
        text = decode(str(raw).encode("ascii", "replace"), terminator=terminator)
    original = text
    text = _normalize_separators(text)
    if ":" not in text:
        raise ScpiError(f"非法指令(缺少子系统分隔符 ':'): {original!r}")

    subsystem, rest = text.split(":", 1)
    subsystem = subsystem.strip().upper()

    # rest 形如 "STEP 1:3" / "STAR" / "PAGE TEST" / "RESU ?" / "GROU ?"
    if " " in rest:
        node, arg_str = rest.split(" ", 1)
    else:
        node, arg_str = rest, ""
    node = node.strip().upper()
    arg_str = arg_str.strip()

    args = tuple(a for a in arg_str.split(" ") if a != "") if arg_str else ()
    is_query = arg_str.endswith("?") or "?" in args or (args and args[-1].endswith(":?"))

    return ScpiCommand(
        subsystem=subsystem, node=node, args=args, is_query=is_query, raw=original,
    )


# ---------------------------------------------------------------------------
# 校验小工具
# ---------------------------------------------------------------------------


def _check_step(step: int) -> int:
    step = int(step)
    if not (STEP_MIN <= step <= STEP_MAX):
        raise ScpiError(f"测试步必须 {STEP_MIN}-{STEP_MAX}, 得到 {step}")
    return step


def _check_item(item: int) -> int:
    item = int(item)
    if not (ITEM_CODE_MIN <= item <= ITEM_CODE_MAX):
        raise ScpiError(f"测试项目编码必须 {ITEM_CODE_MIN}-{ITEM_CODE_MAX}, 得到 {item}")
    return item


def _check_group(n: int) -> int:
    n = int(n)
    if not (GROUP_MIN <= n <= GROUP_MAX):
        raise ScpiError(f"测试组号必须 {GROUP_MIN:02d}-{GROUP_MAX}, 得到 {n}")
    return n


def _fmt_param(p: Any) -> str:
    """单个 PARA 参数格式化: ``\`` → 0;其余原样(去空白)。"""
    s = str(p).strip()
    if s == NO_ITEM or s == "":
        return "0"
    return s


def normalize_params(params: Sequence[Any]) -> Tuple[str, ...]:
    """把 9 参数固定化: 不足补 0,超出报错,``\`` → 0。"""
    out = [_fmt_param(p) for p in params]
    if len(out) > PARA_PARAM_COUNT:
        raise ScpiError(f"PARA 参数最多 {PARA_PARAM_COUNT} 个, 得到 {len(out)}")
    out += ["0"] * (PARA_PARAM_COUNT - len(out))
    return tuple(out)


# ---------------------------------------------------------------------------
# 构造:结构化意图 → wire 字符串 (不含结束标记)
# ---------------------------------------------------------------------------


def build_safe_start() -> str:
    """SAFE: 启动当前测试。"""
    return "SAFE:STAR"


def build_safe_stop() -> str:
    """SAFE: 停止测试。"""
    return "SAFE:STOP"


def build_disp_page(page: str) -> str:
    """DISP: 切换页面 SYST|FILE|TEST。"""
    page = str(page).strip().upper()
    if page not in DISP_PAGES:
        raise ScpiError(f"DISP:PAGE 仅支持 {DISP_PAGES}, 得到 {page!r}")
    return f"DISP:PAGE {page}"


def build_sour_set_step(step: int, item: int) -> str:
    """SOUR: 设置第 step 步的测试项目 item。 e.g. ``SOUR:STEP 1:3``"""
    return f"SOUR:STEP {_check_step(step)}:{_check_item(item)}"


def build_sour_query_step(step: int) -> str:
    """SOUR: 查询第 step 步的测试项目。 e.g. ``SOUR:STEP 1:?``"""
    return f"SOUR:STEP {_check_step(step)}:?"


def build_para_set_step(step: int, item: int, params: Sequence[Any]) -> str:
    """PARA: 设置测试步 + 9 个固定参数。

    e.g. ``PARA:STEP 1:6 220 0 0 0 4 0.6 2100 0.03 3``
    """
    step = _check_step(step)
    item = _check_item(item)
    fixed = normalize_params(params)
    return f"PARA:STEP {step}:{item} " + " ".join(fixed)


def build_para_query_group() -> str:
    """PARA: 查询当前组 8 步项目。 ``PARA:STEP ?``"""
    return "PARA:STEP ?"


def build_mmem_name(name: str) -> str:
    """MMEM: 设置测试组名。 ``MMEM:NAME xxx``"""
    name = str(name).strip()
    if not name:
        raise ScpiError("MMEM:NAME 名称不能为空")
    if " " in name:
        raise ScpiError("MMEM:NAME 名称不能含空格")
    return f"MMEM:NAME {name}"


def build_mmem_save() -> str:
    """MMEM: 保存 (仅新建测试组用,平时禁用)。"""
    return "MMEM:SAVE"


def build_mmem_status() -> str:
    """MMEM: 查询状态 0 Ready/1 测试中/2 OK/3 NG。"""
    return "MMEM:STAT"


def build_mmem_set_group(n: int) -> str:
    """MMEM: 切组 01-99。 ``MMEM:GROU 01``"""
    return f"MMEM:GROU {_check_group(n):02d}"


def build_mmem_query_group() -> str:
    """MMEM: 查询当前组号。 ``MMEM:GROU ?``"""
    return "MMEM:GROU ?"


def build_fetc_set_auto(on: Any) -> str:
    """FETC: 自动回传 0 关 / 1 开。 ``FETC:AUTO 1``"""
    flag = 1 if (on in (1, "1", True) or str(on).strip().lower() in ("on", "true", "yes")) else 0
    return f"FETC:AUTO {flag}"


def build_fetc_query_auto() -> str:
    """FETC: 查询自动回传开关。 ``FETC:AUTO ?``"""
    return "FETC:AUTO ?"


def build_fetc_query_result() -> str:
    """FETC: 查询全部测试结果。 ``FETC:RESU ?``"""
    return "FETC:RESU ?"


# ---------------------------------------------------------------------------
# 测试步 / 结果 数据结构
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """单步测试结果,对应 RESU 返回的 ``STEP_N1:N2_N3_N4_N5_N6``。"""

    step: int
    item: int
    readings: Tuple[str, str, str] = ("0", "0", "0")  # N3 N4 N5 (物理量随项目而定)
    status: int = STATUS_READY                          # N6

    def to_wire(self) -> str:
        r = list(self.readings) + ["0", "0", "0"]
        return f"STEP {self.step}:{self.item} {r[0]} {r[1]} {r[2]} {self.status}"


# ---------------------------------------------------------------------------
# 仪器仿真:对指令给出应答
# ---------------------------------------------------------------------------


@dataclass
class _Group:
    name: str = ""
    # step_no -> (item, params(9))
    steps: Dict[int, Tuple[int, Tuple[str, ...]]] = field(default_factory=dict)


class LinoSafetyTester:
    """LINO 安规测试仪仿真器:维护状态并对指令给出符合 PDF 示例格式的应答。

    既用于自测,也作为 :class:`SCPIProtocol` 的 ``loopback`` 传输后端,
    让整条采集链路无需真机即可跑通。
    """

    def __init__(self) -> None:
        self.groups: Dict[int, _Group] = {1: _Group()}
        self.current_group: int = 1
        self.status: int = STATUS_READY
        self.auto: int = 0  # 开机默认关
        self.page: str = "TEST"
        self.results: Dict[int, StepResult] = {}
        self.log = logging.getLogger(f"{__name__}.LinoSafetyTester")

    # ---- 内部 ---- #
    def _group(self) -> _Group:
        return self.groups.setdefault(self.current_group, _Group())

    # ---- 主入口 ---- #
    def handle(self, raw: Any, *, terminator: bytes = TERMINATOR_NL) -> Optional[bytes]:
        """处理一条指令。查询类返回应答 bytes;设置类返回 None。

        非法指令抛 :class:`ScpiError` (调用方决定是否吞掉)。
        """
        cmd = parse_command(raw, terminator=terminator)
        text = self._dispatch(cmd)
        if text is None:
            return None
        return encode(text, terminator=terminator)

    def _dispatch(self, cmd: ScpiCommand) -> Optional[str]:
        handler = getattr(self, f"_h_{cmd.subsystem.lower()}", None)
        if handler is None:
            raise ScpiError(f"未知子系统: {cmd.subsystem!r}")
        return handler(cmd)

    # ---- SAFE ---- #
    def _h_safe(self, cmd: ScpiCommand) -> Optional[str]:
        if cmd.node == "STAR":
            self.status = STATUS_TESTING
            self.results = {}
            return None
        if cmd.node == "STOP":
            if self.status == STATUS_TESTING:
                self.status = STATUS_READY
            return None
        raise ScpiError(f"未知 SAFE 节点: {cmd.node!r}")

    # ---- DISP ---- #
    def _h_disp(self, cmd: ScpiCommand) -> Optional[str]:
        if cmd.node == "PAGE":
            if not cmd.args or cmd.args[0].upper() not in DISP_PAGES:
                raise ScpiError(f"DISP:PAGE 非法参数: {cmd.args}")
            self.page = cmd.args[0].upper()
            return None
        raise ScpiError(f"未知 DISP 节点: {cmd.node!r}")

    # ---- SOUR ---- #
    def _h_sour(self, cmd: ScpiCommand) -> Optional[str]:
        if cmd.node != "STEP":
            raise ScpiError(f"未知 SOUR 节点: {cmd.node!r}")
        # args[0] 形如 "1:3" (设置) 或 "1:?" (查询)
        if not cmd.args:
            raise ScpiError("SOUR:STEP 缺少参数")
        step_str, _, item_str = cmd.args[0].partition(":")
        step = _check_step(step_str)
        if item_str == "?":
            grp = self._group()
            item = grp.steps.get(step, (0, ()))[0]
            return f"STEP {step}:{item}"
        item = _check_item(item_str)
        grp = self._group()
        prev = grp.steps.get(step)
        params = prev[1] if prev else tuple(["0"] * PARA_PARAM_COUNT)
        grp.steps[step] = (item, params)
        return None

    # ---- PARA ---- #
    def _h_para(self, cmd: ScpiCommand) -> Optional[str]:
        if cmd.node != "STEP":
            raise ScpiError(f"未知 PARA 节点: {cmd.node!r}")
        if cmd.is_query or (cmd.args and cmd.args[0] == "?"):
            # 查询当前组 8 步项目: STEP 1:N1 2:N2 ... 8:N8
            grp = self._group()
            parts = [f"{s}:{grp.steps.get(s, (0, ()))[0]}" for s in range(STEP_MIN, STEP_MAX + 1)]
            return "STEP " + " ".join(parts)
        # 设置: args[0]="1:6", args[1:]=9 参数
        if not cmd.args:
            raise ScpiError("PARA:STEP 缺少参数")
        step_str, _, item_str = cmd.args[0].partition(":")
        step = _check_step(step_str)
        item = _check_item(item_str)
        params = normalize_params(cmd.args[1:])
        self._group().steps[step] = (item, params)
        return None

    # ---- MMEM ---- #
    def _h_mmem(self, cmd: ScpiCommand) -> Optional[str]:
        if cmd.node == "NAME":
            if not cmd.args:
                raise ScpiError("MMEM:NAME 缺少名称")
            self._group().name = cmd.args[0]
            return None
        if cmd.node == "SAVE":
            # 平时禁用;仿真器接受但不持久化。
            return None
        if cmd.node == "STAT":
            return str(self.status)
        if cmd.node == "GROU":
            if cmd.args and cmd.args[0] == "?":
                return f"{self.current_group:02d}"
            if not cmd.args:
                raise ScpiError("MMEM:GROU 缺少组号")
            self.current_group = _check_group(cmd.args[0])
            self.groups.setdefault(self.current_group, _Group())
            return None
        raise ScpiError(f"未知 MMEM 节点: {cmd.node!r}")

    # ---- FETC ---- #
    def _h_fetc(self, cmd: ScpiCommand) -> Optional[str]:
        if cmd.node == "AUTO":
            if cmd.args and cmd.args[0] == "?":
                return str(self.auto)
            if not cmd.args:
                raise ScpiError("FETC:AUTO 缺少参数")
            self.auto = 1 if str(cmd.args[0]).strip() == "1" else 0
            return None
        if cmd.node == "RESU":
            return self._format_results()
        raise ScpiError(f"未知 FETC 节点: {cmd.node!r}")

    def _format_results(self) -> str:
        """按 ``STEP_N1:N2_N3_N4_N5_N6`` 拼全部已编程测试步的结果, ``;`` 分隔。"""
        grp = self._group()
        steps = sorted(s for s, (item, _) in grp.steps.items() if item != 0)
        lines: List[str] = []
        for s in steps:
            item = grp.steps[s][0]
            res = self.results.get(s) or StepResult(step=s, item=item)
            lines.append(res.to_wire())
        return RESULT_SEP.join(lines) if lines else "STEP 0:0 0 0 0 0"

    # ---- 测试便捷方法 (供仿真器消费方注入结果) ---- #
    def load_result(self, step: int, item: int, readings: Sequence[str], status: int) -> None:
        r = tuple(list(readings)[:3] + ["0", "0", "0"])[:3]
        self.results[_check_step(step)] = StepResult(
            step=int(step), item=_check_item(item), readings=r, status=int(status)
        )


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------


class _LoopbackTransport:
    """把指令回环到内置 :class:`LinoSafetyTester`,用于 dev / 测试 (无真机)。"""

    def __init__(self, simulator: Optional[LinoSafetyTester] = None,
                 terminator: bytes = TERMINATOR_NL) -> None:
        self.sim = simulator or LinoSafetyTester()
        self.terminator = terminator
        self._pending: Optional[bytes] = None

    def write(self, data: bytes) -> None:
        self._pending = self.sim.handle(data, terminator=self.terminator)

    def read_response(self) -> Optional[bytes]:
        resp, self._pending = self._pending, None
        return resp

    def close(self) -> None:
        self._pending = None


class _SerialTransport:
    """pyserial 传输:写指令、按结束标记读应答。"""

    def __init__(self, ser: Any, terminator: bytes = TERMINATOR_NL) -> None:
        self.ser = ser
        self.terminator = terminator

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def read_response(self) -> Optional[bytes]:
        # 读到结束标记为止 (pyserial Serial.read_until)。
        line = self.ser.read_until(self.terminator)
        return line if line else None

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 采集适配:SCPIProtocol(BaseProtocol)
# ---------------------------------------------------------------------------

#: 测点 query 类型 → 对应查询构造器。
_POINT_QUERIES = {
    "status": build_mmem_status,      # MMEM:STAT  → 0/1/2/3
    "auto": build_fetc_query_auto,    # FETC:AUTO ? → 0/1
    "group": build_mmem_query_group,  # MMEM:GROU ? → 01-99
    "result": build_fetc_query_result,  # FETC:RESU ? → 全部步结果(原文)
    "step_status": None,              # FETC:RESU ? 后取指定 step 的单步状态(需 step 字段)
}


@ProtocolRegistry.register("scpi", "scpi_serial", "lino")
class SCPIProtocol(BaseProtocol):
    """LINO 安规测试仪 SCPI 串口协议 (沙永健工厂)。

    ``serial_port=loopback`` 时使用内置仿真器,可在无真机环境跑通采集与自测。
    """

    META = ProtocolMeta(
        name="scpi",
        label="SCPI 串口 (LINO 安规测试仪)",
        category="fieldbus",
        description="LINO 安规测试仪 ASCII 串口指令集 (SAFE/DISP/SOUR/PARA/MMEM/FETC)",
        supports_pause=True,
    )

    DEVICE_FIELDS = (
        FieldSpec("serial_port", "串口设备", required=True,
                  help_text="如 /dev/ttyUSB0 或 COM3;填 loopback 走内置仿真器", example="/dev/ttyUSB0"),
        FieldSpec("baudrate", "波特率", kind="int", default=9600,
                  help_text="常见值: 9600 / 19200 / 38400 / 115200"),
        FieldSpec("parity", "校验位", kind="enum", choices=("N", "E", "O"), default="N",
                  help_text="N=无, E=偶校验, O=奇校验"),
        FieldSpec("bytesize", "数据位", kind="enum", choices=(7, 8), default=8),
        FieldSpec("stopbits", "停止位", kind="enum", choices=(1, 2), default=1),
        FieldSpec("timeout", "超时(秒)", kind="float", default=2.0),
        FieldSpec("group", "测试组号", kind="int", default=0,
                  help_text="连接后切到该组 (01-99);0=不切组"),
        FieldSpec("use_eoi", "EOI 结束标记", kind="bool", default=False,
                  help_text="开启则指令尾追加 ^END (EOI),默认仅 NL(0x0A)"),
        FieldSpec("auto_result", "自动回传结果", kind="bool", default=False,
                  help_text="连接后下发 FETC:AUTO 1"),
    )
    IDENTITY_FIELDS = ("serial_port",)

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, help_text="测点唯一标识", example="tester_status"),
        FieldSpec("query", "查询类型", kind="enum",
                  choices=tuple(_POINT_QUERIES.keys()), default="status",
                  help_text="status=仪器状态 / auto=自动回传开关 / group=当前组号 / "
                            "result=全部结果原文 / step_status=指定步状态"),
        FieldSpec("step", "测试步号", kind="int", default=0,
                  help_text="query=step_status 时取该步(1-8)的单步状态"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "中文名称", example="安规仪状态"),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.serial_port = device_config.get("serial_port")
        self.baudrate = int(device_config.get("baudrate", 9600))
        self.parity = str(device_config.get("parity", "N"))
        self.bytesize = int(device_config.get("bytesize", 8))
        self.stopbits = int(device_config.get("stopbits", 1))
        self.timeout = float(device_config.get("timeout", 2.0))
        self.group = int(device_config.get("group", 0) or 0)
        self.use_eoi = bool(device_config.get("use_eoi", False))
        self.auto_result = bool(device_config.get("auto_result", False))
        self.terminator = TERMINATOR_NL
        # 注入的仿真器 (测试用) 或 loopback 端口都会走 _LoopbackTransport。
        self._injected_sim: Optional[LinoSafetyTester] = device_config.get("_simulator")
        self.transport: Optional[Any] = None

    # ---- Lifecycle ---- #
    def _is_loopback(self) -> bool:
        return self._injected_sim is not None or str(self.serial_port).lower() == "loopback"

    def connect(self) -> bool:
        try:
            if self._is_loopback():
                self.transport = _LoopbackTransport(self._injected_sim, terminator=self.terminator)
            else:
                import serial  # 本地导入 —— 只有真机用户付依赖代价

                ser = serial.Serial(
                    port=self.serial_port,
                    baudrate=self.baudrate,
                    parity=self.parity,
                    bytesize=self.bytesize,
                    stopbits=self.stopbits,
                    timeout=self.timeout,
                )
                self.transport = _SerialTransport(ser, terminator=self.terminator)
            self.is_connected = True
            # 连接后可选切组 / 开自动回传。
            if self.group:
                self.send(build_mmem_set_group(self.group))
            if self.auto_result:
                self.send(build_fetc_set_auto(1))
            self.logger.info("Connected SCPI %s @ %d baud (loopback=%s)",
                             self.serial_port, self.baudrate, self._is_loopback())
            return True
        except Exception as exc:  # noqa: BLE001
            self.is_connected = False
            raise ConnectionError(f"SCPI connection failed: {exc}") from exc

    def disconnect(self) -> None:
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.is_connected = False

    # ---- 收发原语 ---- #
    def send(self, text: str) -> None:
        """下发一条设置类指令 (不期待应答)。"""
        if not self.is_connected or self.transport is None:
            raise ReadError("SCPI 未连接")
        self.transport.write(encode(text, use_eoi=self.use_eoi, terminator=self.terminator))

    def query(self, text: str) -> str:
        """下发一条查询类指令并返回去掉结束标记的应答正文。"""
        if not self.is_connected or self.transport is None:
            raise ReadError("SCPI 未连接")
        self.transport.write(encode(text, use_eoi=self.use_eoi, terminator=self.terminator))
        resp = self.transport.read_response()
        if resp is None:
            raise ReadError(f"SCPI 查询无应答: {text!r}")
        return decode(resp, terminator=self.terminator)

    # ---- 控制类便捷方法 (超出 BaseProtocol 读接口,供上层下发指令) ---- #
    def start_test(self) -> None:
        self.send(build_safe_start())

    def stop_test(self) -> None:
        self.send(build_safe_stop())

    def set_page(self, page: str) -> None:
        self.send(build_disp_page(page))

    def set_step_item(self, step: int, item: int) -> None:
        self.send(build_sour_set_step(step, item))

    def set_step_params(self, step: int, item: int, params: Sequence[Any]) -> None:
        self.send(build_para_set_step(step, item, params))

    def switch_group(self, n: int) -> None:
        self.send(build_mmem_set_group(n))
        self.group = int(n)

    def read_status(self) -> int:
        return int(self.query(build_mmem_status()))

    def read_results_raw(self) -> str:
        return self.query(build_fetc_query_result())

    def read_results(self) -> List[StepResult]:
        """解析 RESU 应答成结构化单步结果列表。"""
        return parse_results(self.read_results_raw())

    # ---- 采集读点 ---- #
    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        import time

        now_ns = time.time_ns()
        out: List[Dict[str, Any]] = []
        for p in points:
            code = p.get("code")
            qtype = str(p.get("query", "status"))
            try:
                value = self._read_one(qtype, p)
                out.append({"code": code, "value": value, "timestamp": now_ns, "quality": "good"})
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("SCPI 读点 %s(%s) 失败: %s", code, qtype, exc)
                out.append({"code": code, "value": None, "timestamp": now_ns, "quality": "bad"})
        return out

    def _read_one(self, qtype: str, p: Dict[str, Any]) -> Any:
        if qtype == "status":
            return int(self.query(build_mmem_status()))
        if qtype == "auto":
            return int(self.query(build_fetc_query_auto()))
        if qtype == "group":
            return int(self.query(build_mmem_query_group()))
        if qtype == "result":
            return self.query(build_fetc_query_result())
        if qtype == "step_status":
            step = _check_step(p.get("step", 0) or 0)
            results = parse_results(self.query(build_fetc_query_result()))
            for r in results:
                if r.step == step:
                    return r.status
            return None
        raise ReadError(f"未知 query 类型: {qtype!r}")

    def health_check(self) -> bool:
        if not self.is_connected or self.transport is None:
            return False
        try:
            resp = self.query(build_mmem_status())
            return resp.strip() in {"0", "1", "2", "3"}
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# RESU 应答解析 (controller 侧)
# ---------------------------------------------------------------------------


def parse_results(raw: str) -> List[StepResult]:
    """把 ``FETC:RESU ?`` 应答 (多步, ``;`` 分隔) 解析成 :class:`StepResult` 列表。

    单步形如 ``STEP 1:3 0 0 0 2`` (N1=步 N2=项目 N3..N5=读数 N6=状态)。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = decode(raw)
    text = _normalize_separators(str(raw))
    results: List[StepResult] = []
    for chunk in str(text).split(RESULT_SEP):
        # 单步形如 "STEP 1:3 2100 0.01 0 2" —— 注意这里 STEP 是节点,没有 SUBSYS: 前缀,
        # 故不能复用 parse_command,直接按 token 拆。
        tokens = chunk.strip().split(" ")
        if len(tokens) < 2 or tokens[0].upper() != "STEP":
            continue
        step_str, _, item_str = tokens[1].partition(":")
        try:
            step = int(step_str)
            item = int(item_str)
        except ValueError:
            continue
        rest = [t for t in tokens[2:] if t != ""]
        status = int(rest[-1]) if rest else STATUS_READY
        readings = tuple((rest[:-1] + ["0", "0", "0"])[:3])
        if step == 0 and item == 0:
            continue  # 占位空结果
        results.append(StepResult(step=step, item=item, readings=readings, status=status))
    return results
