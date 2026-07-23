"""一个采集任务最多绑一台设备。

设备与任务是多对一:一台设备可有多个任务,一个任务只能属于一台设备。
这样删设备就能干净地连带删掉它的所有任务(级联信号),不会留下空壳任务。
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from configuration import models
from tests.fixtures.factories import *  # noqa: F401,F403


@pytest.mark.django_db
class TestSingleDevicePerTask:
    def test_reject_task_spanning_two_devices(self, create_device, create_point):
        d1 = create_device(code="DEV_A")
        d2 = create_device(code="DEV_B")
        p1 = create_point(device=d1, code="p1")
        p2 = create_point(device=d2, code="p2")

        resp = APIClient().post("/api/config/tasks/", {
            "code": "cross-dev", "name": "跨设备", "sample_rate_hz": 1,
            "points": [p1.id, p2.id],
        }, format="json")

        assert resp.status_code == 400
        assert "一台设备" in str(resp.data)

    def test_accept_task_on_single_device(self, create_device, create_point):
        d = create_device(code="DEV_ONE")
        pts = [create_point(device=d, code=f"p{i}") for i in range(3)]

        resp = APIClient().post("/api/config/tasks/", {
            "code": "one-dev", "name": "单设备", "sample_rate_hz": 1,
            "points": [p.id for p in pts],
        }, format="json")

        assert resp.status_code == 201, resp.data
        assert models.AcqTask.objects.get(code="one-dev").points.count() == 3

    def test_one_device_many_tasks(self, create_device, create_point):
        """一台设备可以挂多个任务。"""
        d = create_device(code="DEV_MULTI")
        p = create_point(device=d, code="p1")
        client = APIClient()
        for code in ("t1", "t2", "t3"):
            r = client.post("/api/config/tasks/", {
                "code": code, "name": code, "sample_rate_hz": 1, "points": [p.id],
            }, format="json")
            assert r.status_code == 201, r.data
        assert models.AcqTask.objects.filter(points__device=d).distinct().count() == 3

    def test_delete_device_cascades_all_its_tasks(
        self, create_device, create_point, create_task,
    ):
        """删设备 → 它的所有任务一并删除(核心诉求)。"""
        d = create_device(code="DEV_DEL")
        pts = [create_point(device=d, code=f"p{i}") for i in range(2)]
        t1 = create_task(code="del-t1", points=[pts[0]])
        t2 = create_task(code="del-t2", points=[pts[1]])
        # 另一台设备的任务不受影响
        other = create_device(code="DEV_KEEP")
        op = create_point(device=other, code="op")
        keep = create_task(code="keep-t", points=[op])

        d.delete()

        assert not models.AcqTask.objects.filter(id__in=[t1.id, t2.id]).exists()
        assert models.AcqTask.objects.filter(id=keep.id).exists()
