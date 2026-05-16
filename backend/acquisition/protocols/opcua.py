"""OPC-UA protocol implementation (synchronous facade over asyncua).

OPC-UA is the modern interop standard for industrial systems. Each point is
addressed by its NodeId (e.g. ``ns=2;s=Channel1.Device1.Tag1`` or ``i=85``).
The synchronous client reuses one event loop per protocol instance so the
acquisition loop (which itself is synchronous) can call ``read_points``
without leaking threads.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)

try:
    from asyncua import Client as AsyncUAClient
    from asyncua import ua
    _OPCUA_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncUAClient = None
    ua = None
    _OPCUA_AVAILABLE = False


_SECURITY_POLICIES = (
    "None",
    "Basic256Sha256",
    "Aes128Sha256RsaOaep",
    "Aes256Sha256RsaPss",
)


class _AsyncRunner:
    """Owns a dedicated thread + event loop so we can make `await` calls
    from the synchronous acquisition loop without polluting the caller."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def close(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)
        finally:
            self.loop.close()


@ProtocolRegistry.register("opcua", "opc-ua", "opc_ua")
class OPCUAProtocol(BaseProtocol):
    META = ProtocolMeta(
        name="opcua",
        label="OPC-UA",
        category="opc",
        description="OPC 基金会的通用工业互联协议;按 NodeId 寻址,支持安全策略",
    )

    DEVICE_FIELDS = (
        FieldSpec("endpoint_url", "Endpoint URL", required=True,
                  example="opc.tcp://192.168.1.50:4840",
                  help_text="OPC-UA 服务端点,以 opc.tcp:// 开头"),
        FieldSpec("security_policy", "安全策略", kind="enum",
                  choices=_SECURITY_POLICIES, default="None"),
        FieldSpec("opcua_username", "用户名", default=""),
        FieldSpec("opcua_password", "密码", kind="secret", default=""),
        FieldSpec("timeout", "超时(秒)", kind="float", default=5.0),
    )
    IDENTITY_FIELDS = ("endpoint_url",)

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, example="motor_speed"),
        FieldSpec("address", "NodeId", required=True,
                  example="ns=2;s=Channel1.Device1.Tag1",
                  help_text="OPC-UA 节点 ID,例如 'ns=2;s=...' 或 'i=85'"),
        FieldSpec("data_type", "数据类型", kind="enum",
                  choices=("auto", "bool", "int16", "uint16", "int32", "uint32", "float32", "double", "string"),
                  default="auto",
                  help_text="auto = 让 OPC-UA 自己解析"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "中文名称", default=""),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.endpoint = device_config.get("endpoint_url")
        self.username = device_config.get("opcua_username") or None
        self.password = device_config.get("opcua_password") or None
        self.security = device_config.get("security_policy", "None")
        self.timeout = float(device_config.get("timeout", 5.0))
        self.client: Optional[Any] = None
        self._runner: Optional[_AsyncRunner] = None

    # ------------- async helpers ------------- #
    async def _async_connect(self) -> None:
        client = AsyncUAClient(self.endpoint, timeout=self.timeout)
        if self.username:
            client.set_user(self.username)
            client.set_password(self.password or "")
        if self.security and self.security != "None":
            await client.set_security_string(f"{self.security},Sign,,")
        await client.connect()
        self.client = client

    async def _async_disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None

    async def _async_read(self, node_ids: List[str]) -> List[tuple]:
        """Read each node independently and normalise failures.

        ``client.read_values`` is a single batch call: one unresolvable
        NodeId (or one node the server rejects) makes it raise and the
        whole cycle's data is lost. Reading node-by-node instead isolates
        the failure to the offending point. Returns a list aligned with
        ``node_ids`` where each element is ``(value, None)`` on success or
        ``(None, error_message)`` on failure.
        """
        if self.client is None:
            raise ReadError("OPC-UA client not connected")
        out: List[tuple] = []
        for nid in node_ids:
            try:
                node = self.client.get_node(nid)
                value = await node.read_value()
                out.append((value, None))
            except Exception as exc:  # noqa: BLE001
                out.append((None, str(exc)))
        return out

    # ------------- sync facade ------------- #
    def connect(self) -> bool:
        if not _OPCUA_AVAILABLE:
            raise ConnectionError("asyncua 未安装,无法连接 OPC-UA")
        try:
            if self._runner is None:
                self._runner = _AsyncRunner()
            self._runner.run(self._async_connect())
            self.is_connected = True
            self.logger.info("Connected to OPC-UA %s", self.endpoint)
            return True
        except Exception as exc:  # noqa: BLE001
            self.is_connected = False
            raise ConnectionError(f"OPC-UA connection failed: {exc}") from exc

    def disconnect(self) -> None:
        if self._runner:
            try:
                self._runner.run(self._async_disconnect())
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("OPC-UA disconnect error: %s", exc)
            finally:
                self._runner.close()
                self._runner = None
                self.is_connected = False

    def health_check(self) -> bool:
        return self.is_connected and self.client is not None

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.is_connected:
            if not self.connect():
                raise ReadError("Not connected to OPC-UA server")

        node_ids = [str(p.get("address", "")) for p in points]
        try:
            outcomes = self._runner.run(self._async_read(node_ids))
        except Exception as exc:  # noqa: BLE001
            # A failure here is transport-level (timeout / dropped session),
            # not a single bad point — surface it so the caller reconnects.
            raise ReadError(f"OPC-UA read failed: {exc}") from exc

        results = []
        failures = 0
        for point, (value, error) in zip(points, outcomes):
            if error is None:
                quality = "good"
            else:
                quality = "bad"
                value = None
                failures += 1
                self.logger.warning(
                    "OPC-UA read failed for %s @ %s: %s",
                    point.get("code"), point.get("address"), error,
                )
            results.append({
                "code": point["code"],
                "value": value,
                "timestamp": time.time_ns(),
                "quality": quality,
                "address": point.get("address"),
            })
        # Every point failing means the server/session is unhealthy — raise
        # so the caller treats it as a transport failure and reconnects
        # instead of streaming an all-bad batch.
        if points and failures == len(points):
            raise ReadError(f"OPC-UA read failed for all {len(points)} point(s)")
        return results
