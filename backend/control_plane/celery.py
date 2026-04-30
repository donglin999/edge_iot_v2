"""Celery application configuration for the control_plane project."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")

app = Celery("control_plane")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Route tasks to dedicated queues so long-running acquisition tasks
# don't starve short-lived ones.
app.conf.task_routes = {
    "acquisition.tasks.start_acquisition_task": {"queue": "acquisition"},
    "acquisition.tasks.stop_acquisition_task": {"queue": "acquisition"},
    "acquisition.tasks.acquire_once": {"queue": "short"},
    "acquisition.tasks.check_protocol_connection": {"queue": "short"},
    "acquisition.tasks.check_storage_connection": {"queue": "short"},
    "configuration.tasks.process_excel_import": {"queue": "short"},
}
# Unrouted tasks fall into the default queue; pin it to 'short' so any
# stray task lands on the responsive worker rather than the acquisition one.
app.conf.task_default_queue = "short"


@app.task(bind=True)
def debug_task(self):  # pragma: no cover - helper task
    print(f"Request: {self.request!r}")
