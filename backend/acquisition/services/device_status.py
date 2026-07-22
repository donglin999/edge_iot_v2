"""设备在线状态 —— 从系统真正知道的事实推出来。

背景:设备列表的「状态」列长期是写死的绿色「在线」,不查任何数据,所以一台
从没连通过的设备也显示在线。这个字段比没有还糟 —— 它让人以为采集正常。

系统其实是知道设备连不连得上的,只是没人把它拼起来:

* ``Alarm(category="connectivity", dedup_key="connectivity:<code>")``
  由 ``ReadWorker`` 在健康→掉线的那一刻落库,恢复时清除(见
  ``pipeline.py::_raise_offline_alarm`` / ``_clear_offline_alarm``)。
* ``AcquisitionSession`` 记录哪个任务正在跑;设备通过 ``task.points.device``
  关联到会话。

于是三态:

===========  ==========================================================
``online``   设备在运行中的会话里,且没有未清除的连接告警
``offline``  有未清除的连接告警 —— 采集在跑但连不上
``idle``     不在任何运行中的会话里 —— 系统压根没在连它,谈不上在线
===========  ==========================================================

**不做主动探测**:列表页渲染时去逐台开 TCP,一屏 50 台设备就是 50 次阻塞
I/O,页面会卡死,而且探测本身还会打扰正在采集的连接。状态只反映采集链路的
既有观测结果 —— 想要主动探测有设备详情页的「测试连接」。
"""
from __future__ import annotations

from typing import Dict, Iterable, Sequence

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_IDLE = "idle"


def compute_device_statuses(devices: Sequence) -> Dict[int, str]:
    """批量算出一组设备的状态。

    固定 2 条查询,与设备数量无关 —— 列表页不能因为多了几台设备就多几十次
    查询。传空序列时不查库。

    Args:
        devices: Device 实例序列(只用到 ``id`` 与 ``code``)。

    Returns:
        ``{device_id: status}``,取值见模块文档。
    """
    if not devices:
        return {}

    # 本地导入:configuration 与 acquisition 互相引用,模块级导入会成环。
    from acquisition import models as acq_models

    device_ids = [d.id for d in devices]
    codes = [d.code for d in devices]

    # 1) 正在跑的会话覆盖到哪些设备。paused 也算「在采集中」——
    #    暂停是人为的,连接状态依然有意义。
    active_device_ids = set(
        acq_models.AcquisitionSession.objects.filter(
            status__in=(
                acq_models.AcquisitionSession.STATUS_RUNNING,
                acq_models.AcquisitionSession.STATUS_PAUSED,
            ),
            task__points__device_id__in=device_ids,
        )
        .values_list("task__points__device_id", flat=True)
        .distinct()
    )

    # 2) 哪些设备有未清除的连接告警。已确认(acked)仍然是没恢复,算离线。
    offline_codes = set(
        acq_models.Alarm.objects.filter(
            category="connectivity",
            device_code__in=codes,
            status__in=(
                acq_models.Alarm.STATUS_FIRING,
                acq_models.Alarm.STATUS_ACKED,
            ),
        ).values_list("device_code", flat=True)
    )

    statuses: Dict[int, str] = {}
    for device in devices:
        if device.code in offline_codes:
            statuses[device.id] = STATUS_OFFLINE
        elif device.id in active_device_ids:
            statuses[device.id] = STATUS_ONLINE
        else:
            statuses[device.id] = STATUS_IDLE
    return statuses


def attach_statuses(devices: Iterable) -> Dict[int, str]:
    """``compute_device_statuses`` 的便捷包装,接受任意可迭代对象。"""
    return compute_device_statuses(list(devices))
