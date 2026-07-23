"""起一个真的 Modbus TCP mock 服务器 + 一组 modbus_tcp 设备,用于无硬件联调。

    python manage.py run_modbus_mock                 # 3 台设备,起服务器并自动启动采集
    python manage.py run_modbus_mock --devices 5 --rate 2
    python manage.py run_modbus_mock --seed-only     # 只建配置和服务器,不自动启动

和 simulator 的关键区别:被采对象是**真的 Modbus TCP 服务器**(modbus_tk),采集
走真正的 ModbusTCPProtocol —— 真开 TCP、真发功能码、真解码字节序和多寄存器。
只有这样才查得出「float32 被当 uint16 读半个值」这类只在 modbus 解码路径暴露的
问题;simulator 永远返回 good,查不出来。

架构:本命令**托管 mock 硬件**(前台阻塞,持有 Modbus 服务器),真正的采集由
celery worker 执行(它连到本机的 mock 服务器)。所以运行前请确保:
  1. django + celery worker 已按 stack 环境变量起好(连 redis/influx)
  2. 本命令自身也要能连库(同一套 DJANGO_DB_NAME)

一台服务器带 N 个从站(slave_id 1..N),N 台设备各连一个从站。每台设备一个任务
(遵守「一设备一任务」)。Ctrl-C 停止:关服务器并把会话置为 stopped。
"""
from __future__ import annotations

import signal
import sys
import time
import urllib.request
import json

from django.core.management.base import BaseCommand
from django.db import transaction

# mock 服务器暴露的测点定义。
from acquisition.testing.modbus_mock_server import ModbusMockServer

API_BASE = "http://127.0.0.1:8000/api"


class Command(BaseCommand):
    help = "起 Modbus TCP mock 服务器 + modbus_tcp 设备,用于无硬件联调(走真实 modbus 解码)"

    def add_arguments(self, parser):
        parser.add_argument("--devices", type=int, default=3, help="设备台数(默认 3)")
        parser.add_argument("--rate", type=float, default=1.0, help="采集频率 Hz(默认 1)")
        parser.add_argument("--host", default="127.0.0.1", help="mock 服务器绑定地址")
        parser.add_argument("--port", type=int, default=15020, help="mock 服务器端口")
        parser.add_argument("--site", default="default", help="站点编码")
        parser.add_argument("--prefix", default="mbmock", help="设备编码前缀")
        parser.add_argument("--seed-only", action="store_true",
                            help="只建配置并起服务器,不自动启动采集")
        parser.add_argument("--no-start", action="store_true",
                            help="不通过 API 自动启动采集(等价 seed-only 但仍阻塞持有服务器)")

    # ------------------------------------------------------------------
    def _say(self, text, style=None):
        self.stdout.write(style(text) if style else text)
        self.stdout.flush()

    def handle(self, *args, **opts):
        n = max(int(opts["devices"]), 1)
        host, port = opts["host"], int(opts["port"])
        slave_ids = list(range(1, n + 1))

        # 1) 建配置(设备 + 每台一个任务)
        task_ids = self._seed(
            n=n, host=host, port=port, rate=float(opts["rate"]),
            site_code=opts["site"], prefix=opts["prefix"],
        )

        # 2) 起 mock 服务器(N 从站)
        server = ModbusMockServer(host=host, port=port, slave_ids=slave_ids)
        server.start()
        self._say(self.style.SUCCESS(
            f"Modbus TCP mock 服务器已启动 {host}:{port},{n} 个从站(slave 1..{n})"
        ))
        time.sleep(1.0)  # 让服务器 socket 就绪再启动采集

        started = []
        if not (opts["seed_only"] or opts["no_start"]):
            # 3) 通过 API 启动采集(celery worker 执行,连到本机 mock 服务器)
            for tid in task_ids:
                if self._start_task(tid):
                    started.append(tid)
            self._say(self.style.SUCCESS(
                f"已通过 API 启动 {len(started)}/{len(task_ids)} 个采集任务(celery worker 执行)"
            ))
        else:
            self._say("已跳过自动启动 —— 到前端「采集控制」手动启动各任务。")

        self._say("")
        self._say("现在可以到前端看:")
        self._say("  · 设备管理    modbus 设备应为「在线」")
        self._say("  · 采集控制    入库速率(真实 modbus 解码:温度/湿度 uint16、压力 float32、计数 int32)")
        self._say("  · 数据可视化  能看到曲线")
        self._say("")
        self._say("按 Ctrl-C 停止(关闭 mock 服务器并停止会话)")

        # 4) 阻塞持有服务器,直到 Ctrl-C
        stop = {"flag": False}

        def _handle(signum, frame):  # noqa: ARG001
            stop["flag"] = True

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        try:
            while not stop["flag"]:
                time.sleep(0.5)
        finally:
            self._say("\n正在停止…")
            server.stop()
            for tid in started:
                self._stop_task_sessions(tid)
            self._say(self.style.SUCCESS("已停止 mock 服务器并收敛会话。"))
            sys.stdout.flush()

    # ------------------------------------------------------------------ 建配置
    @transaction.atomic
    def _seed(self, *, n, host, port, rate, site_code, prefix):
        from configuration import models

        site, _ = models.Site.objects.get_or_create(
            code=site_code, defaults={"name": site_code, "description": "modbus mock 站点"},
        )
        # 清掉本前缀的旧设备(级联删掉其任务)
        old = models.Device.objects.filter(code__startswith=f"{prefix}-")
        if old.exists():
            self._say(f"清理 {old.count()} 台旧 modbus mock 设备…")
            old.delete()

        task_ids = []
        for i in range(1, n + 1):
            device = models.Device.objects.create(
                site=site,
                code=f"{prefix}-{i:02d}",
                name=f"Modbus 设备 {i:02d}",
                protocol="modbus_tcp",
                ip_address=host,
                port=port,
                metadata={
                    "source_ip": host, "source_port": port,
                    "slave_id": i, "byte_order": "big", "timeout": 3.0,
                },
            )
            points = []
            for pd in ModbusMockServer.POINTS:
                p = models.Point.objects.create(
                    device=device, code=pd["code"], address=pd["address"],
                    description=pd["desc"],
                    extra={
                        "data_type": pd["data_type"], "num": pd["num"],
                        "function_code": 3, "unit": pd["unit"], "protocol": "modbus_tcp",
                    },
                )
                points.append(p)
            # 一设备一任务
            task = models.AcqTask.objects.create(
                code=f"{prefix}-task-{i:02d}", name=f"Modbus 采集 {i:02d}",
                description="run_modbus_mock 生成", sample_rate_hz=rate, is_active=True,
            )
            task.points.set(points)
            task_ids.append(task.id)

        self._say(self.style.SUCCESS(
            f"配置就绪:{n} 台 modbus_tcp 设备 × {len(ModbusMockServer.POINTS)} 测点,"
            f"{n} 个任务(一设备一任务)@ {rate}Hz"
        ))
        return task_ids

    # ------------------------------------------------------------------ API
    def _start_task(self, task_id) -> bool:
        try:
            req = urllib.request.Request(
                f"{API_BASE}/acquisition/sessions/start-task/",
                data=json.dumps({"task_id": task_id}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status in (200, 201)
        except Exception as exc:  # noqa: BLE001
            self._say(self.style.WARNING(f"  启动任务 {task_id} 失败: {exc}(celery worker 起了吗?)"))
            return False

    def _stop_task_sessions(self, task_id) -> None:
        from acquisition import models as acq
        for s in acq.AcquisitionSession.objects.filter(
            task_id=task_id, status=acq.AcquisitionSession.STATUS_RUNNING,
        ):
            acq.AcquisitionSession.objects.filter(id=s.id).update(status="stopped")
