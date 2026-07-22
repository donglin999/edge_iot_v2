"""连接测试的分步追踪。

以前「测试连接」只回一个 ``{success, message}``:界面上按下去要等好几秒才蹦一个
提示,失败了也只知道「失败」,不知道是配置就不全、还是 TCP 没通、还是连上了但
握手不过。排障时这三种情况的处理完全不同。

这里把测试拆成有名字的几步,每步各自记录结果、耗时、以及一句给人看的说明,
最后连同总结一起返回。界面据此把整个过程画出来,而不是一个转圈。

步骤是**协议无关**的:每一步做什么由协议自己的实现决定(``connect`` /
``health_check``),这里只负责计时、归类和兜异常 —— 所以新增协议不用改这里。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_WARNING = "warning"


class _Trace:
    """按顺序累积步骤结果。"""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []

    def record(
        self,
        key: str,
        label: str,
        status: str,
        detail: str = "",
        duration_ms: float = 0.0,
    ) -> Dict[str, Any]:
        step = {
            "key": key,
            "label": label,
            "status": status,
            "detail": detail,
            "duration_ms": round(duration_ms, 1),
        }
        self.steps.append(step)
        return step

    def run(self, key: str, label: str, fn: Callable[[], str]) -> bool:
        """跑一步。``fn`` 返回给人看的说明;抛异常即该步失败。

        Returns:
            True 表示这步过了,调用方据此决定要不要继续往下走。
        """
        started = time.perf_counter()
        try:
            detail = fn() or ""
        except Exception as exc:  # noqa: BLE001 - 任何异常都只是这一步失败
            self.record(
                key, label, STATUS_FAILED, str(exc),
                (time.perf_counter() - started) * 1000,
            )
            return False
        self.record(
            key, label, STATUS_OK, detail,
            (time.perf_counter() - started) * 1000,
        )
        return True

    def skip(self, key: str, label: str, why: str) -> None:
        self.record(key, label, STATUS_SKIPPED, why)


def trace_connection(
    protocol_type: str,
    device_config: Dict[str, Any],
    *,
    device_code: str = "",
) -> Dict[str, Any]:
    """跑一次分步连接测试。

    这个函数**从不抛异常** —— 任何失败都体现为某一步的 ``failed``,
    调用方(Celery 任务 / 视图)拿到的永远是一份完整的过程记录。

    Args:
        protocol_type: 协议名。
        device_config: 已经过 ``build_device_config`` 分层合成的完整配置。
        device_code: 仅用于日志与展示。

    Returns:
        ``{success, protocol, device_code, steps: [...], summary, total_ms}``
    """
    from acquisition.protocols import ProtocolRegistry

    trace = _Trace()
    started = time.perf_counter()
    protocol = None

    # ---- 1. 配置检查 ---------------------------------------------------
    def _check_config() -> str:
        klass = ProtocolRegistry.get(protocol_type)
        missing = [
            f"{f.label}({f.name})"
            for f in getattr(klass, "DEVICE_FIELDS", ())
            if f.required and device_config.get(f.name) in (None, "")
        ]
        if missing:
            # 配置就不全的话,后面几步做了也没意义 —— 直接判这步失败。
            raise ValueError("缺少必填配置: " + "、".join(missing))
        identity = ", ".join(
            f"{name}={device_config.get(name)}"
            for name in getattr(klass, "IDENTITY_FIELDS", ())
        )
        return f"{klass.META.label} · {identity}" if identity else klass.META.label

    if not trace.run("config", "检查设备配置", _check_config):
        trace.skip("connect", "建立连接", "配置不完整,未尝试连接")
        trace.skip("handshake", "握手/健康检查", "未连接")
        trace.skip("disconnect", "断开连接", "未连接")
        return _finish(trace, protocol_type, device_code, started, connected=False)

    # ---- 2. 建立连接 ---------------------------------------------------
    def _connect() -> str:
        nonlocal protocol
        protocol = ProtocolRegistry.create(protocol_type, device_config)
        # 各协议的 connect 语义不一:有的返回 bool,有的失败直接抛。
        ok = protocol.connect()
        if ok is False or not getattr(protocol, "is_connected", False):
            raise ConnectionError("协议报告未连接")
        return "已建立连接"

    connected = trace.run("connect", "建立连接", _connect)

    # ---- 3. 握手 / 健康检查 --------------------------------------------
    if connected:
        started_hc = time.perf_counter()
        try:
            healthy = bool(protocol.health_check())
            elapsed = (time.perf_counter() - started_hc) * 1000
            if healthy:
                trace.record("handshake", "握手/健康检查", STATUS_OK,
                             "设备响应正常", elapsed)
            else:
                # 连上了但设备不响应 —— 和连不上是两码事,单独标出来。
                trace.record("handshake", "握手/健康检查", STATUS_WARNING,
                             "已连接,但设备未通过健康检查(可能是从站地址/寄存器不对)",
                             elapsed)
        except Exception as exc:  # noqa: BLE001
            trace.record("handshake", "握手/健康检查", STATUS_FAILED, str(exc),
                         (time.perf_counter() - started_hc) * 1000)
    else:
        trace.skip("handshake", "握手/健康检查", "未连接,跳过")

    # ---- 4. 断开 -------------------------------------------------------
    if connected and protocol is not None:
        trace.run("disconnect", "断开连接", lambda: (protocol.disconnect(), "已释放")[1])
    else:
        trace.skip("disconnect", "断开连接", "未连接,无需断开")

    return _finish(trace, protocol_type, device_code, started, connected=connected)


def _finish(
    trace: _Trace,
    protocol_type: str,
    device_code: str,
    started: float,
    *,
    connected: bool,
) -> Dict[str, Any]:
    handshake = next((s for s in trace.steps if s["key"] == "handshake"), None)
    failed = [s for s in trace.steps if s["status"] == STATUS_FAILED]

    success = not failed and handshake is not None and handshake["status"] == STATUS_OK
    if failed:
        summary = f"{failed[0]['label']}失败:{failed[0]['detail']}"
    elif handshake is not None and handshake["status"] == STATUS_WARNING:
        summary = "已连接,但设备未通过健康检查"
    elif success:
        summary = "连接正常"
    else:
        summary = "未能完成连接测试"

    return {
        "success": success,
        "protocol": protocol_type,
        "device_code": device_code,
        "connected": connected,
        "steps": trace.steps,
        "summary": summary,
        "total_ms": round((time.perf_counter() - started) * 1000, 1),
    }
