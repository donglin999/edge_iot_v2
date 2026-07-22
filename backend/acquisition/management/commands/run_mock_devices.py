"""一条命令把一组模拟设备跑起来,用来在没有现场硬件时联调整条链路。

    python manage.py run_mock_devices              # 建 3 台设备并开始采集
    python manage.py run_mock_devices --devices 5 --rate 2
    python manage.py run_mock_devices --fail read  # 演练:采集在跑但读不到 → 告警 + 离线
    python manage.py run_mock_devices --seed-only  # 只建配置,不启动采集

走的是和真设备完全一样的路径(BaseProtocol → ReadWorker → sink → InfluxDB),
只有最底下那层 I/O 是编的。所以前端看到的在线状态、曲线、告警、自动重连,
就是真实行为。

Ctrl-C 停止:会把会话置为 stopped,前端设备状态随即回到离线。
"""
from __future__ import annotations

import os
import signal
import sys

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# 必须在导入协议注册表之前打开开关 —— 模拟协议默认不注册。
os.environ.setdefault("EDGE_ENABLE_SIMULATOR", "1")

DEFAULT_POINTS = [
    ("temperature", "温度", "℃", "float"),
    ("pressure", "压力", "MPa", "float"),
    ("speed", "转速", "rpm", "float"),
    ("running", "运行状态", "", "bool"),
]

WAVEFORMS = ["sine", "ramp", "step", "random", "constant"]


class Command(BaseCommand):
    help = "启动一组模拟设备并真实采集,用于无硬件联调"

    def add_arguments(self, parser):
        parser.add_argument("--devices", type=int, default=3, help="模拟设备台数(默认 3)")
        parser.add_argument("--points", type=int, default=4,
                            help=f"每台设备的测点数,最多 {len(DEFAULT_POINTS)}(默认 4)")
        parser.add_argument("--rate", type=float, default=1.0, help="采集频率 Hz(默认 1)")
        parser.add_argument("--site", default="default", help="站点编码(默认 default)")
        parser.add_argument("--task", default="mock-task", help="采集任务编码")
        parser.add_argument("--prefix", default="mock", help="设备编码前缀")
        parser.add_argument(
            "--fail", default="none", choices=("none", "connect", "read", "flaky"),
            help="故障注入:connect 连不上 / read 读失败 / flaky 间歇失败",
        )
        parser.add_argument("--seed-only", action="store_true", help="只建配置,不启动采集")
        parser.add_argument("--reset", action="store_true",
                            help="先删掉本前缀下已有的模拟设备与任务")

    # ------------------------------------------------------------------
    def _say(self, text: str, style=None) -> None:
        """写一行并**立刻刷出去**。

        这个命令跑起来就长期阻塞在采集循环里,而 Python 在非终端(nohup / 管道 /
        IDE 终端)下会缓冲 stdout —— 不显式 flush 的话,启动提示要等到进程退出
        才一次性吐出来,用户看到的是一片空白。
        """
        self.stdout.write(style(text) if style else text)
        self.stdout.flush()

    # ------------------------------------------------------------------
    def handle(self, *args, **opts):
        from acquisition.protocols import ProtocolRegistry
        from acquisition.protocols.simulator import register_if_enabled

        if not register_if_enabled():
            raise CommandError("模拟协议未启用 —— 请设置 EDGE_ENABLE_SIMULATOR=1")
        if "simulator" not in ProtocolRegistry._protocols:  # pragma: no cover
            raise CommandError("模拟协议注册失败")

        device_count = max(int(opts["devices"]), 1)
        point_count = min(max(int(opts["points"]), 1), len(DEFAULT_POINTS))

        if opts["reset"]:
            self._reset(opts["prefix"], opts["task"])

        task = self._seed(
            site_code=opts["site"],
            task_code=opts["task"],
            prefix=opts["prefix"],
            device_count=device_count,
            point_count=point_count,
            rate=float(opts["rate"]),
            fail_mode=opts["fail"],
        )

        if opts["seed_only"]:
            self._say(self.style.SUCCESS(
                f"已建好 {device_count} 台模拟设备 / 任务 {task.code}(未启动采集)。\n"
                f"到前端「采集控制」页启动,或去掉 --seed-only 重跑。"
            ))
            return

        self._check_influx()
        self._run(task, fail_mode=opts["fail"])

    # ------------------------------------------------------------------ 前置检查
    def _check_influx(self) -> None:
        """先探一次 InfluxDB。

        写不进去时,采集本身照常跑(状态/告警/连接测试都不受影响),只有历史曲线
        是空的。但如果不提前说清楚,现象就是「一切正常却没有图」,外加满屏 401
        —— 与其让人自己去猜,不如在这里把该设的环境变量列出来。
        """
        from django.conf import settings

        url = f"http://{settings.INFLUXDB_HOST}:{settings.INFLUXDB_PORT}"
        try:
            import requests

            resp = requests.get(
                f"{url}/api/v2/buckets",
                params={"limit": 1},
                headers={"Authorization": f"Token {settings.INFLUXDB_TOKEN}"},
                timeout=3,
            )
            ok = resp.status_code == 200
            reason = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, str(exc)

        if ok:
            self._say(self.style.SUCCESS(
                f"InfluxDB 可写:{url} org={settings.INFLUXDB_ORG} "
                f"bucket={settings.INFLUXDB_BUCKET}"
            ))
            return

        self._say(self.style.WARNING(
            f"⚠ InfluxDB 写入不可用({url}:{reason})——采集会照常跑,"
            f"设备状态、告警、连接测试都正常,但「数据可视化」不会有曲线。\n"
            f"  要看曲线,请带上这套环境变量重跑:\n"
            f"    INFLUXDB_HOST=127.0.0.1 INFLUXDB_PORT=8086 \\\n"
            f"    INFLUXDB_TOKEN=<你的 token> INFLUXDB_ORG=<org> INFLUXDB_BUCKET=<bucket> \\\n"
            f"    python manage.py run_mock_devices\n"
            f"  当前用的是 org={settings.INFLUXDB_ORG!r} bucket={settings.INFLUXDB_BUCKET!r}"
            f" token={'已设置' if settings.INFLUXDB_TOKEN else '空'}"
        ))

    # ------------------------------------------------------------------ 建配置
    def _reset(self, prefix: str, task_code: str) -> None:
        from configuration import models

        devices = models.Device.objects.filter(code__startswith=f"simulator-{prefix}")
        n = devices.count()
        devices.delete()
        models.AcqTask.objects.filter(code=task_code).delete()
        self._say(f"已清理 {n} 台旧的模拟设备与任务 {task_code}")

    @transaction.atomic
    def _seed(self, *, site_code, task_code, prefix, device_count, point_count,
              rate, fail_mode):
        from configuration import models

        site, _ = models.Site.objects.get_or_create(
            code=site_code, defaults={"name": site_code, "description": "模拟设备站点"},
        )

        all_points = []
        for i in range(1, device_count + 1):
            tag = f"{prefix}-{i:02d}"
            device, _ = models.Device.objects.update_or_create(
                code=f"simulator-{tag}",
                defaults={
                    "site": site,
                    "name": f"模拟设备 {i:02d}",
                    "protocol": "simulator",
                    "ip_address": tag,
                    "port": None,
                    "metadata": {
                        "source_ip": tag,
                        # 每台给不同波形,图表上一眼能分辨
                        "sim_waveform": WAVEFORMS[(i - 1) % len(WAVEFORMS)],
                        "sim_period_s": 60.0,
                        "sim_amplitude": 20.0 + 10.0 * ((i - 1) % 3),
                        "sim_baseline": 50.0,
                        "sim_fail_mode": fail_mode,
                        "sim_fail_rate": 0.3,
                    },
                },
            )
            for code, desc, unit, data_type in DEFAULT_POINTS[:point_count]:
                point, _ = models.Point.objects.update_or_create(
                    device=device, code=code,
                    defaults={
                        "address": "",
                        "description": desc,
                        "sample_rate_hz": rate,
                        "extra": {"data_type": data_type, "unit": unit,
                                  "description": desc, "protocol": "simulator"},
                    },
                )
                all_points.append(point)

        task, _ = models.AcqTask.objects.update_or_create(
            code=task_code,
            defaults={"name": "模拟设备采集", "description": "由 run_mock_devices 生成",
                      "sample_rate_hz": rate, "is_active": True},
        )
        task.points.set(all_points)

        self._say(self.style.SUCCESS(
            f"配置就绪:{device_count} 台设备 × {point_count} 个测点 = {len(all_points)} 点,"
            f" 任务 {task.code} @ {rate}Hz,故障注入={fail_mode}"
        ))
        return task

    # ------------------------------------------------------------------ 跑
    def _run(self, task, *, fail_mode):
        from acquisition import models as acq_models
        from acquisition.services.acquisition_service import AcquisitionService

        session = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            celery_task_id="run_mock_devices",
            metadata={"source": "run_mock_devices"},
        )

        self._say("")
        self._say(self.style.SUCCESS("采集已启动 —— 现在可以到前端看:"))
        self._say("  · 连接与测点  设备状态应变为「在线」")
        self._say("  · 数据可视化  能看到曲线")
        self._say("  · 采集控制    会话在运行")
        if fail_mode != "none":
            self._say(self.style.WARNING(
                f"  · 告警中心    故障注入={fail_mode},稍候会出现连接告警,设备转「离线」"
            ))
        self._say("")
        # 开关是按进程生效的:这个命令自己设了,但 web 进程没有 —— 那边不注册
        # simulator 的话,协议下拉里看不到它、编辑设备和「测试连接」都会报
        # 「Protocol 'simulator' not registered」。很容易在这儿卡住。
        self._say(self.style.WARNING(
            "注意:web / celery 进程也要带上同一个开关,否则前端「测试连接」和"
            "设备编辑会报 Protocol 'simulator' not registered:\n"
            "    EDGE_ENABLE_SIMULATOR=1 python manage.py runserver"
        ))
        self._say("")
        self._say("按 Ctrl-C 停止(会把会话置为 stopped,设备随即回到离线)")
        self._say("")

        def _stop(signum, frame):  # noqa: ARG001
            self._say("\n正在停止…")
            acq_models.AcquisitionSession.objects.filter(id=session.id).update(
                status=acq_models.AcquisitionSession.STATUS_STOPPED,
            )

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            result = AcquisitionService(task, session).run_continuous()
        finally:
            # 无论怎么退出,都别把会话留在 RUNNING —— 否则前端会一直显示在线。
            acq_models.AcquisitionSession.objects.filter(
                id=session.id, status=acq_models.AcquisitionSession.STATUS_RUNNING,
            ).update(status=acq_models.AcquisitionSession.STATUS_STOPPED)

        self._say(self.style.SUCCESS(f"已停止:{result.get('status')}"))
        sys.stdout.flush()
