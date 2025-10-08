"""ASGI config for control_plane project."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")

application = get_asgi_application()
