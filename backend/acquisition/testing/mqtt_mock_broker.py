"""真实 mosquitto broker 起停封装 —— 无硬件/无云端联调用。

和 ``modbus_mock_server.py`` 的定位一致:mock 的是**对端**(broker),协议侧走
真正的 ``MQTTProtocol``/``SCADAProtocol`` —— 真 TCP、真 CONNECT/CONNACK、真
SUBSCRIBE、真收 PUBLISH,解析走协议自己的 ``_parse_message``/``_extract_value``。

用 ``docker run eclipse-mosquitto`` 起一个匿名可连的 broker,容器内固定监听
1883,宿主机端口默认 18883(明文,1883 已被 ssh 隧道占用,不能碰)。

已知的 docker-desktop-on-mac 坑:单文件 bind mount 到 ``/tmp/...`` 下的路径会
在 arm64+qemu 场景下被 daemon 误认成目录(``Config file ... is a directory``);
配置文件必须放在 ``/Users/...`` 下的路径才能正常挂载,所以这里用仓库内的
``.scratch/`` 目录暂存生成的 mosquitto.conf,而不是 ``tempfile.mkdtemp()``
(默认落在 /tmp)。
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

# 专用容器名/端口,**必须**与长驻开发 broker(edge-test-mosquitto @18883,
# host_protocol_mocks 和实机联调用)分开:本助手 start() 会 rm -f 同名孤儿容器、
# stop() 也会删容器 —— 曾两次把长驻 broker 连锅端掉(全量测试跑完 broker 消失,
# 实机采集莫名断连)。测试自生自灭,不碰长驻实例。
CONTAINER_NAME = "edge-test-mosquitto-pytest"
HOST_PORT = 18893
IMAGE = "eclipse-mosquitto:2"

# Repo-relative scratch dir (NOT /tmp — see module docstring for why).
_CONF_DIR = Path(__file__).resolve().parents[2] / ".scratch" / "mqtt-mock-broker"


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class MosquittoMockBroker:
    """起停一个真实的 mosquitto docker 容器,监听 ``host_port``(默认 18883)。

    用法::

        broker = MosquittoMockBroker()
        broker.start()
        try:
            ...  # 用 broker.host / broker.host_port 连接
            broker.docker_stop()   # 模拟断线
            broker.docker_start()  # 模拟恢复
        finally:
            broker.stop()          # 彻底清理,不留孤儿容器/配置文件
    """

    host = "127.0.0.1"

    def __init__(
        self,
        host_port: int = HOST_PORT,
        container_name: str = CONTAINER_NAME,
        allow_anonymous: bool = True,
    ) -> None:
        self.host_port = host_port
        self.container_name = container_name
        self.allow_anonymous = allow_anonymous
        self._conf_path: Optional[Path] = None
        self._started = False

    # ------------------------------------------------------------------
    def start(self, timeout: float = 20.0) -> None:
        if not docker_available():
            raise RuntimeError("docker 不可用")
        # 起之前先清掉同名的孤儿容器(上次异常退出可能留下的)。
        self._force_remove()

        _CONF_DIR.mkdir(parents=True, exist_ok=True)
        self._conf_path = _CONF_DIR / f"{self.container_name}.conf"
        self._conf_path.write_text(
            "listener 1883\n"
            f"allow_anonymous {'true' if self.allow_anonymous else 'false'}\n"
            "persistence false\n"
            "log_dest stdout\n"
        )

        # 端口冲突时重试几个备用端口,不占用别的 agent/服务的端口段。
        last_exc: Optional[Exception] = None
        for port in (self.host_port, self.host_port + 1, self.host_port + 2):
            try:
                self._run(port)
                self.host_port = port
                self._started = True
                self._wait_ready(timeout)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._force_remove()
        raise RuntimeError(f"mosquitto 容器启动失败: {last_exc}")

    def _run(self, port: int) -> None:
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "-p", f"{port}:1883",
            "-v", f"{self._conf_path}:/edge-test/mosquitto.conf:ro",
            IMAGE, "mosquitto", "-c", "/edge-test/mosquitto.conf",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.strip()}")

    def _force_remove(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True, text=True, timeout=15,
        )

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _port_open(self.host, self.host_port):
                return
            # 容器早退(比如配置炸了)就没必要接着等端口了。
            status = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name],
                capture_output=True, text=True, timeout=10,
            )
            if status.returncode == 0 and status.stdout.strip() == "false":
                logs = subprocess.run(
                    ["docker", "logs", self.container_name],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                raise RuntimeError(f"mosquitto 容器启动后退出了。日志:\n{logs}")
            time.sleep(0.3)
        raise RuntimeError(f"mosquitto 容器 {self.container_name} 在 {timeout}s 内未就绪")

    # ------------------------------------------------------------------ 断连模拟
    def docker_stop(self, timeout: float = 15.0) -> None:
        """临时停容器(不删),用于测协议侧的断连收敛。"""
        subprocess.run(
            ["docker", "stop", self.container_name],
            capture_output=True, text=True, timeout=timeout,
        )

    def docker_start(self, timeout: float = 20.0) -> None:
        """把 :meth:`docker_stop` 停掉的容器重新拉起。"""
        subprocess.run(
            ["docker", "start", self.container_name],
            capture_output=True, text=True, timeout=timeout,
        )
        self._wait_ready(timeout)

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """彻底清理:删容器、删临时配置文件。可靠地在 finally 里调用。"""
        if self._started:
            self._force_remove()
            self._started = False
        if self._conf_path is not None:
            self._conf_path.unlink(missing_ok=True)
            self._conf_path = None
