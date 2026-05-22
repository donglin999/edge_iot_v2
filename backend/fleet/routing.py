"""WebSocket routing for the fleet control-plane."""
from django.urls import re_path

from .consumers import FleetConsumer, FleetTaskStatusConsumer

websocket_urlpatterns = [
    # edge-agent ↔ center control plane
    re_path(r"^ws/fleet/$", FleetConsumer.as_asgi()),
    # browser /acquisition ← center live task-status feed (M3)
    re_path(r"^ws/fleet/task-statuses/$", FleetTaskStatusConsumer.as_asgi()),
]
