"""Initialize Celery app on Django startup."""
from .celery import app as celery_app

__all__ = ("celery_app",)
