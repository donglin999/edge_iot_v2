"""Seed the disposable M0 SQLite source with a known API/WS/alarm/data fixture."""
from django.utils import timezone

from acquisition.models import AcquisitionSession, Alarm, DataPoint
from configuration.models import AcqTask, Site


site, _ = Site.objects.update_or_create(
    code="M0-CI-SITE",
    defaults={"name": "M0 recovery drill", "description": "disposable CI fixture"},
)
task, _ = AcqTask.objects.update_or_create(
    code="M0-CI-TASK",
    defaults={"name": "M0 recovery acquisition fixture", "is_active": True},
)
session, _ = AcquisitionSession.objects.update_or_create(
    task=task,
    celery_task_id="m0-ci-fixture",
    defaults={
        # Paused remains visible to REST and the global WebSocket, but cannot
        # be mistaken for an orphan and auto-restarted during a slow archive.
        "status": AcquisitionSession.STATUS_PAUSED,
        "started_at": timezone.now(),
        "metadata": {"total_points_read": 1, "drill": True},
    },
)
DataPoint.objects.update_or_create(
    session=session,
    point_code="M0-CI-POINT",
    timestamp=timezone.now(),
    defaults={"value": 42.5, "quality": "good", "metadata": {"drill": True}},
)
Alarm.objects.update_or_create(
    dedup_key="m0-ci-recovery-alarm",
    status=Alarm.STATUS_FIRING,
    defaults={
        "session": session,
        "category": "system",
        "severity": "warning",
        "point_code": "M0-CI-POINT",
        "device_code": "M0-CI-DEVICE",
        "value": {"known": 42.5},
        "message": "Disposable M0 recovery alarm fixture",
    },
)
assert Site.objects.filter(code="M0-CI-SITE").count() == 1
assert DataPoint.objects.filter(point_code="M0-CI-POINT").count() == 1
assert Alarm.objects.filter(dedup_key="m0-ci-recovery-alarm").count() == 1
print(f"seeded disposable recovery fixture (session={session.pk})")
