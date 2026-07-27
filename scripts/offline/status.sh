#!/usr/bin/env bash
# 采集状态速查:活跃会话 + 每设备健康(状态/连续失败/队列丢弃) + 最近告警。
set -euo pipefail
cd "$(dirname "$0")"

docker compose exec -T celery-acq python - <<'PY'
import os
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")
django.setup()

from acquisition.models import AcquisitionSession, Alarm

active = AcquisitionSession.objects.filter(
    status__in=["running", "paused", "starting"]
).select_related("task")
if not active:
    print("没有活跃会话(未启动或已停止)。启动: ./import-and-start.sh")
for s in active:
    md = s.metadata or {}
    print(f"[会话#{s.id}] {s.task.code}  状态={s.status}  "
          f"累计入库={md.get('total_points_read', 0)}  "
          f"入库速率={md.get('ingest_points_per_sec', '-')}/s")
    for code, h in (md.get("device_health") or {}).items():
        age = ""
        if h.get("last_success"):
            age = f"  最近成功读={int(time.time() - h['last_success'])}s前"
        drops = h.get("dropped_messages", 0)
        drop_s = f"  ⚠队列丢弃={drops}" if drops else ""
        print(f"   {code}: {h.get('status')}  连续失败={h.get('consecutive_failures', 0)}{age}{drop_s}")

firing = Alarm.objects.filter(status="firing").order_by("-id")[:5]
if firing:
    print("\n未清除告警:")
    for a in firing:
        print(f"   [{a.category}/{a.severity}] {a.message[:90]}")
PY
