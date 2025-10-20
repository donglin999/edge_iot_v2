"""WebSocket routing for acquisition module."""
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/acquisition/sessions/(?P<session_id>\d+)/$', consumers.AcquisitionConsumer.as_asgi()),
    re_path(r'ws/acquisition/global/$', consumers.GlobalAcquisitionConsumer.as_asgi()),
]
