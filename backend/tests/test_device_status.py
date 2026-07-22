"""设备在线状态 —— 之前这一列是写死的绿色「在线」。

设备列表的状态列长期硬编码为 ``<Badge status="success" text="在线" />``,
不查任何数据:一台从没连通过、甚至没有任何采集任务的设备,照样显示在线。
这比没有这一列更糟 —— 它让人以为采集正常。

系统其实一直知道设备连不连得上(``ReadWorker`` 在掉线时落一条
``category="connectivity"`` 的告警,恢复时清除),只是没人把它接到列表上。

这组用例锁住推导规则本身,以及一条容易在重构中失守的性能约束:
状态计算必须是固定条数的查询,不能随设备数增长。
"""
from __future__ import annotations

import pytest

from acquisition import models as acq_models
from acquisition.services.device_status import (
    STATUS_IDLE,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    compute_device_statuses,
)
from tests.fixtures.factories import *  # noqa: F401,F403


def _connectivity_alarm(device, status=acq_models.Alarm.STATUS_FIRING, session=None):
    return acq_models.Alarm.objects.create(
        category="connectivity",
        severity="critical",
        device_code=device.code,
        point_code="",
        dedup_key=f"connectivity:{device.code}",
        value={},
        status=status,
        session=session,
        message="连接失败",
    )


@pytest.mark.django_db
def test_device_without_any_task_is_idle_not_online(create_device):
    """核心回归:没有任何采集任务的设备,绝不能显示「在线」。"""
    device = create_device()
    assert compute_device_statuses([device]) == {device.id: STATUS_IDLE}


@pytest.mark.django_db
def test_device_in_running_session_is_online(create_point, create_task, create_session):
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task, status=acq_models.AcquisitionSession.STATUS_RUNNING)

    assert compute_device_statuses([point.device]) == {point.device.id: STATUS_ONLINE}


@pytest.mark.django_db
def test_paused_session_still_counts_as_acquiring(create_point, create_task, create_session):
    """暂停是人为的,连接状态依然有意义,不该掉回「未采集」。"""
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task, status=acq_models.AcquisitionSession.STATUS_PAUSED)

    assert compute_device_statuses([point.device])[point.device.id] == STATUS_ONLINE


@pytest.mark.django_db
def test_stopped_session_falls_back_to_idle(create_point, create_task, create_session):
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task, status=acq_models.AcquisitionSession.STATUS_STOPPED)

    assert compute_device_statuses([point.device])[point.device.id] == STATUS_IDLE


@pytest.mark.django_db
def test_firing_connectivity_alarm_makes_device_offline(
    create_point, create_task, create_session
):
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task)
    _connectivity_alarm(point.device)

    assert compute_device_statuses([point.device])[point.device.id] == STATUS_OFFLINE


@pytest.mark.django_db
def test_acknowledged_alarm_is_still_offline(create_point, create_task, create_session):
    """确认过 ≠ 恢复了 —— 人点了「已确认」不代表设备连上了。"""
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task)
    _connectivity_alarm(point.device, status=acq_models.Alarm.STATUS_ACKED)

    assert compute_device_statuses([point.device])[point.device.id] == STATUS_OFFLINE


@pytest.mark.django_db
def test_cleared_alarm_returns_to_online(create_point, create_task, create_session):
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task)
    _connectivity_alarm(point.device, status=acq_models.Alarm.STATUS_CLEARED)

    assert compute_device_statuses([point.device])[point.device.id] == STATUS_ONLINE


@pytest.mark.django_db
def test_offline_wins_over_running_session(create_point, create_task, create_session):
    """采集在跑但连不上,该说离线 —— 不能因为「任务在运行」就报在线。"""
    point = create_point()
    task = create_task(points=[point])
    session = create_session(task=task)
    _connectivity_alarm(point.device, session=session)

    assert compute_device_statuses([point.device])[point.device.id] == STATUS_OFFLINE


@pytest.mark.django_db
def test_alarm_of_another_device_does_not_leak(create_device, create_point, create_task, create_session):
    """按 device_code 精确匹配,别的设备离线不能连累这台。"""
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task)
    other = create_device(code="SOMEONE_ELSE")
    _connectivity_alarm(other)

    statuses = compute_device_statuses([point.device, other])
    assert statuses[point.device.id] == STATUS_ONLINE
    assert statuses[other.id] == STATUS_OFFLINE


@pytest.mark.django_db
def test_empty_input_does_not_hit_the_database(django_assert_num_queries):
    with django_assert_num_queries(0):
        assert compute_device_statuses([]) == {}


@pytest.mark.django_db
def test_query_count_is_constant_regardless_of_device_count(
    create_site, create_device, django_assert_num_queries
):
    """列表页不能因为多了几十台设备就多几十次查询。"""
    site = create_site()
    few = [create_device(site=site, code=f"D{i}") for i in range(2)]
    with django_assert_num_queries(2):
        compute_device_statuses(few)

    many = few + [create_device(site=site, code=f"D{i}") for i in range(2, 40)]
    with django_assert_num_queries(2):
        compute_device_statuses(many)


# ===========================================================================
# API 层:列表真的把状态带出去了
# ===========================================================================


@pytest.mark.django_db
def test_device_list_api_exposes_real_status(client, create_point, create_task, create_session):
    point = create_point()
    task = create_task(points=[point])
    create_session(task=task)

    response = client.get("/api/config/devices/")
    assert response.status_code == 200
    payload = response.json()
    rows = payload.get("results", payload)
    row = next(r for r in rows if r["id"] == point.device.id)
    assert row["status"] == STATUS_ONLINE


@pytest.mark.django_db
def test_device_list_api_reports_idle_for_unused_device(client, create_device):
    """接口层的回归:凭空的设备必须是 idle,不能是 online。"""
    device = create_device()

    response = client.get("/api/config/devices/")
    rows = response.json().get("results", response.json())
    row = next(r for r in rows if r["id"] == device.id)
    assert row["status"] == STATUS_IDLE
