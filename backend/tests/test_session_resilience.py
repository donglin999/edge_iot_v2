"""会话韧性:瞬时 DB 错误/单 worker 死亡不能杀掉健康的采集会话。

现场事故(中山 muju,2026-07-28):SQLite 锁竞争下心跳写失败 + refresh 抛错,
健康会话每 ~120s 被「正常完成」再由看门狗拉起,无限重启循环。
"""
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from acquisition import models as acq_models
from acquisition.services.acquisition_service import AcquisitionService
from configuration import models as config_models


@pytest.fixture
def service(db):
    site = config_models.Site.objects.create(code="s1", name="S1")
    device = config_models.Device.objects.create(
        site=site, code="d1", name="D1", protocol="scada", ip_address="127.0.0.1",
    )
    task = config_models.AcqTask.objects.create(code="t1", name="T1", sample_rate_hz=1)
    session = acq_models.AcquisitionSession.objects.create(
        task=task, status=acq_models.AcquisitionSession.STATUS_RUNNING,
    )
    svc = AcquisitionService(task, session)
    svc._test_device = device
    return svc


@pytest.mark.django_db
class TestShouldContinueResilience:
    def test_transient_refresh_error_does_not_stop_session(self, service):
        with mock.patch.object(service.session, "refresh_from_db",
                               side_effect=Exception("database is locked")):
            for _ in range(AcquisitionService._MAX_REFRESH_FAILURES - 1):
                assert service._should_continue() is True

    def test_persistent_refresh_error_eventually_gives_up(self, service):
        with mock.patch.object(service.session, "refresh_from_db",
                               side_effect=Exception("database is locked")):
            results = [service._should_continue()
                       for _ in range(AcquisitionService._MAX_REFRESH_FAILURES)]
        assert results[-1] is False
        assert all(results[:-1])

    def test_success_resets_failure_counter(self, service):
        boom = mock.patch.object(service.session, "refresh_from_db",
                                 side_effect=Exception("locked"))
        with boom:
            for _ in range(AcquisitionService._MAX_REFRESH_FAILURES - 1):
                service._should_continue()
        # 一次成功清零
        assert service._should_continue() is True
        assert service._refresh_failures == 0
        with boom:
            assert service._should_continue() is True  # 重新从 1 计

    def test_deleted_session_stops_immediately(self, service):
        with mock.patch.object(
            service.session, "refresh_from_db",
            side_effect=acq_models.AcquisitionSession.DoesNotExist,
        ):
            assert service._should_continue() is False


class TestSqliteTuning:
    def test_wal_and_busy_timeout_applied(self, db):
        from django.db import connection
        with connection.cursor() as c:
            c.execute("PRAGMA journal_mode;")
            mode = c.fetchone()[0]
            c.execute("PRAGMA busy_timeout;")
            busy = c.fetchone()[0]
        # 内存库(测试)WAL 不可用返回 memory,文件库应为 wal —— 两者都接受,
        # busy_timeout 必须生效。
        assert mode in ("wal", "memory")
        assert int(busy) >= 30000
