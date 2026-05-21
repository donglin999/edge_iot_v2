"""WebSocket routing for the fleet control-plane."""
from django.urls import re_path

from .consumers import FleetConsumer

websocket_urlpatterns = [
    re_path(r"^ws/fleet/$", FleetConsumer.as_asgi()),
]
