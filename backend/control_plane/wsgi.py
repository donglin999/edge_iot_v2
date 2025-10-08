"""WSGI config for control_plane project."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")

application = get_wsgi_application()
