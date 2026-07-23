"""模拟设备协议 —— 不接任何硬件,自己造读数。

用来在没有现场设备的情况下把整条链路真跑起来:配置 → 采集 → 入库 → 前端图表 →
断线告警 → 自动重连。走的是和真协议完全一样的路径(``BaseProtocol`` +
``ReadWorker`` + sink),所以看到的行为就是真实行为,只有最底下那层 I/O 是编的。

**默认不注册**。生产环境的协议下拉里不该出现一个假协议 —— 要用得显式打开:

    EDGE_ENABLE_SIMULATOR=1

配套的 ``python manage.py run_mock_devices`` 会自动带上这个开关。

故障注入:``sim_fail_mode`` 可以让它连不上或读不到,用来验证告警与自愈 ——
这是它比真设备好使的地方,真设备你没法说拔就拔。
"""
from __future__ import annotations

import math
import os
import random
import time
from typing import Any, Dict, List

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)

_WAVEFORMS = ("sine", "ramp", "random", "constant", "step")
_FAIL_MODES = ("none", "connect", "read", "flaky")


class SimulatorProtocol(BaseProtocol):
    """按波形生成读数的假设备。

    每个测点的值只由 ``(测点码, 当前时间)`` 决定,所以同一时刻重复读是稳定的,
    而随时间推移会变化 —— 图表上能看出形状,不是一条噪声。
    """

    META = ProtocolMeta(
        name="simulator",
        label="模拟设备",
        category="other",
        description="不接硬件的模拟设备,用于全链路联调与故障演练",
    )

    DEVICE_FIELDS = (
        # required=False: base.py's validator only flags "missing" when BOTH
        # required=True and default is None — with a non-empty default set,
        # required=True was already a no-op here while still drawing a
        # misleading required-field asterisk in the frontend form.
        FieldSpec("source_ip", "标识", required=False, default="sim-1",
                  help_text="仅用于区分不同模拟设备,不会真的去连;留空则用默认值 sim-1",
                  example="sim-1"),
        FieldSpec("sim_waveform", "波形", kind="enum", choices=_WAVEFORMS,
                  default="sine", help_text="sine 正弦 / ramp 锯齿 / random 随机 / constant 恒定 / step 阶跃"),
        FieldSpec("sim_period_s", "周期(秒)", kind="float", default=60.0,
                  help_text="走完一个完整波形所需的秒数"),
        FieldSpec("sim_amplitude", "幅值", kind="float", default=50.0),
        FieldSpec("sim_baseline", "基线", kind="float", default=50.0,
                  help_text="波形围绕这个值上下摆动"),
        FieldSpec("sim_latency_ms", "模拟时延(毫秒)", kind="float", default=0.0,
                  help_text="每次读取假装花掉的时间,用来观察慢设备的表现"),
        FieldSpec("sim_fail_mode", "故障注入", kind="enum", choices=_FAIL_MODES,
                  default="none",
                  help_text="none 正常 / connect 连不上 / read 读失败 / flaky 按概率间歇失败"),
        FieldSpec("sim_fail_rate", "故障概率", kind="float", default=0.3,
                  help_text="仅 flaky 模式生效,0~1"),
    )
    IDENTITY_FIELDS = ("source_ip",)

    POINT_FIELDS = (
        FieldSpec("data_type", "数据类型", kind="enum",
                  choices=("float", "int", "bool"), default="float"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "描述", default=""),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)

        def _num(key: str, default: float) -> float:
            """取数值配置。

            不能写 ``config.get(k) or default``:0 是 falsy,会被悄悄换成默认值 ——
            于是「故障概率设 0」变成 0.3、「基线设 0」变成 50,配了等于没配。
            """
            raw = device_config.get(key)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        self.tag = str(device_config.get("source_ip") or "sim")
        self.waveform = str(device_config.get("sim_waveform") or "sine")
        self.period = max(_num("sim_period_s", 60.0), 0.001)
        self.amplitude = _num("sim_amplitude", 50.0)
        self.baseline = _num("sim_baseline", 50.0)
        self.latency = max(_num("sim_latency_ms", 0.0), 0.0) / 1000.0
        self.fail_mode = str(device_config.get("sim_fail_mode") or "none")
        self.fail_rate = min(max(_num("sim_fail_rate", 0.3), 0.0), 1.0)

    # ------------------------------------------------------------------ 生命周期
    def connect(self) -> bool:
        if self.fail_mode == "connect":
            self.is_connected = False
            raise ConnectionError(f"模拟设备 {self.tag} 拒绝连接(sim_fail_mode=connect)")
        if self.fail_mode == "flaky" and random.random() < self.fail_rate:
            self.is_connected = False
            raise ConnectionError(f"模拟设备 {self.tag} 间歇性连接失败(flaky)")
        self.is_connected = True
        self.logger.info("模拟设备 %s 已连接(%s 波形)", self.tag, self.waveform)
        return True

    def disconnect(self) -> None:
        self.is_connected = False

    def health_check(self) -> bool:
        if self.fail_mode in ("connect", "read"):
            return False
        return self.is_connected

    # ------------------------------------------------------------------ 读取
    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.is_connected:
            raise ReadError(f"模拟设备 {self.tag} 未连接")
        if self.fail_mode == "read":
            raise ReadError(f"模拟设备 {self.tag} 读取失败(sim_fail_mode=read)")
        if self.fail_mode == "flaky" and random.random() < self.fail_rate:
            # 顺带把连接也断掉 —— 现实中的「时好时坏」多半是链路掉了,而不是
            # 连接好端端地在、只有读失败。断了 worker 才会走重连,也才演示得出
            # 「掉线 → 告警 → 自动重连 → 告警清除」这一整圈。
            self.is_connected = False
            raise ReadError(f"模拟设备 {self.tag} 链路中断(flaky)")
        if self.latency:
            time.sleep(self.latency)

        now = time.time()
        now_ns = time.time_ns()
        results: List[Dict[str, Any]] = []
        for point in points:
            code = str(point.get("code", ""))
            value = self._value_for(code, now)
            data_type = str(point.get("data_type", "float")).lower()
            if data_type in ("int", "int16", "uint16", "int32", "uint32"):
                value = int(round(value))
            elif data_type == "bool":
                value = value >= self.baseline
            else:
                value = round(float(value), 3)
            results.append({
                "code": code,
                "value": value,
                "timestamp": now_ns,
                "quality": "good",
                "address": point.get("address", ""),
            })
        return results

    def _value_for(self, code: str, now: float) -> float:
        """按波形算值。

        每个测点用测点码的哈希做相位偏移,这样同一台设备的几个测点不会完全重合,
        图表上看得出是几条不同的曲线。
        """
        phase = (hash(code) % 1000) / 1000.0
        t = ((now / self.period) + phase) % 1.0

        if self.waveform == "sine":
            return self.baseline + self.amplitude * math.sin(2 * math.pi * t)
        if self.waveform == "ramp":
            return self.baseline - self.amplitude + 2 * self.amplitude * t
        if self.waveform == "step":
            return self.baseline + (self.amplitude if t < 0.5 else -self.amplitude)
        if self.waveform == "random":
            return self.baseline + random.uniform(-self.amplitude, self.amplitude)
        return self.baseline  # constant


def register_if_enabled() -> bool:
    """按环境变量决定要不要注册。

    Returns:
        True 表示这次调用后模拟协议是可用的。
    """
    if os.environ.get("EDGE_ENABLE_SIMULATOR", "").lower() not in ("1", "true", "yes", "on"):
        return False
    ProtocolRegistry._protocols.setdefault(SimulatorProtocol.META.name, SimulatorProtocol)
    return True


register_if_enabled()
