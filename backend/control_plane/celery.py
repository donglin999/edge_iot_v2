"""Celery application configuration for the control_plane project."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")

app = Celery("control_plane")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):  # pragma: no cover - helper task
    print(f"Request: {self.request!r}")
