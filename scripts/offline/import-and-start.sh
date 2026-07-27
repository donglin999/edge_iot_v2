#!/usr/bin/env bash
# ============================================================================
# 一键导入博创注塑机 SCADA 配置并启动采集 —— 在 load-and-up.sh 起好服务后运行。
#
#   ./import-and-start.sh                          # 用 config/ 里自带的工作簿
#   ./import-and-start.sh 路径/其他配置.xlsx        # 用指定工作簿
#
# 做三件事(幂等,重复跑不会产生重复设备/测点/任务):
#   1. 把两表工作簿拷进 celery-acq 容器(该容器完整模式/采集模式都在);
#   2. 容器内走与页面导入完全相同的 parse → provision 服务链导入
#      (任务编码/频率取工作簿「网关服务」sheet 的任务三列);
#   3. 对导入产生的每个采集任务下发 start_acquisition_task(自建会话,断线
#      自动重连,工控机重启后 compose 也会自动恢复 RUNNING 会话)。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

WB="${1:-config/博创注塑机-scada导入.xlsx}"
[ -f "$WB" ] || { echo "找不到工作簿: $WB"; exit 1; }

echo "==> [1/3] 拷贝工作簿进容器"
docker compose cp "$WB" celery-acq:/tmp/scada_import.xlsx

echo "==> [2/3] 导入(与页面导入同一条 parse→provision 服务链)"
echo "==> [3/3] 下发采集启动"
docker compose exec -T celery-acq python - <<'PY'
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")
django.setup()

from django.db import transaction

from configuration import models
from configuration.services.scada_excel import build_task_payload, parse_scada_workbook
from configuration.services.scada_provision import provision

parsed = parse_scada_workbook("/tmp/scada_import.xlsx")
if not parsed.is_valid:
    for e in parsed.errors[:10]:
        print(f"  行{e.row} 列{e.column}: {e.message}")
    raise SystemExit("工作簿校验失败,未写入任何数据")

wb_task = parsed.task or {}
task_payload = build_task_payload(
    task_code=wb_task.get("task_code", ""),
    task_name=wb_task.get("task_name", ""),
    sample_rate_hz=wb_task.get("sample_rate_hz"),
)

data = dict(parsed.gateway)
gateway_code = data.pop("code")
with transaction.atomic():
    gateway, _ = models.ScadaGateway.objects.update_or_create(
        code=gateway_code, defaults=data
    )
    result = provision(gateway, {"devices": parsed.devices, "task": task_payload})

c = result["created"]
print(f"导入完成: 新建设备 {c['devices']} / 新建测点 {c['points']}"
      f" (已存在的就地更新,不重复建)")

from acquisition.tasks import start_acquisition_task

for t in result["tasks"]:
    # 幂等:任务已有 RUNNING 会话时,start_acquisition_task 内部守卫直接跳过。
    start_acquisition_task.delay(t["id"])
    print(f"已下发启动: {t['code']} (task_id={t['id']})")
PY

echo ""
echo "==> 完成。约 30 秒后验证:"
echo "    ./status.sh                     # 会话状态 + 每设备健康(含丢弃计数)"
echo "    完整模式下也可打开 http://<工控机IP>/ 在页面上看采集控制台"
