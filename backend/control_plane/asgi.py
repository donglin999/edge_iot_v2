"""
ASGI config for control_plane project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/asgi/
"""
import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application
from django.urls import re_path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

# Import routing after Django is initialized
from acquisition.routing import websocket_urlpatterns as acquisition_ws_urls
from fleet.mqtt_transport import lifespan_handler as fleet_mqtt_lifespan
from fleet.routing import websocket_urlpatterns as fleet_ws_urls

# WebSocket routing is split by audience:
#
# * Operator-UI sockets (``acquisition``) are opened by a browser, so they
#   stay behind ``AllowedHostsOriginValidator`` — that validator is a
#   browser cross-origin / CSRF guard and checks the ``Origin`` header
#   against ``ALLOWED_HOSTS``.
# * Fleet control-plane sockets (``/ws/fleet/``) are opened by the
#   ``edge-agent`` process, NOT a browser. The edge-agent authenticates with
#   an activation token inside the first ``register`` frame (see
#   ``docs/distributed/protocol.md``). It connects from arbitrary LAN IPs,
#   and a non-browser client has no meaningful ``Origin``; running it through
#   ``AllowedHostsOriginValidator`` only rejected legitimate edges (the
#   documented smoke runbook tells operators to use the host LAN IP, which is
#   never in ``ALLOWED_HOSTS``). Origin validation is therefore deliberately
#   omitted for fleet sockets — token auth is the real gate. This does not
#   change the M1 wire protocol; it only removes a deployment-layer guard
#   that was misapplied to a machine-to-machine channel.
acquisition_ws_app = AllowedHostsOriginValidator(
    AuthMiddlewareStack(URLRouter(acquisition_ws_urls))
)

_protocol_router = ProtocolTypeRouter({
    # Django's ASGI application to handle traditional HTTP requests
    "http": django_asgi_app,

    # WebSocket handler — fleet routes are matched first (token-authed,
    # no Origin validation); everything else falls through to the
    # browser-facing, Origin-validated acquisition router.
    "websocket": URLRouter(
        list(fleet_ws_urls)
        + [re_path(r"^", acquisition_ws_app)]
    ),
})


# Phase 2 P1 (XIU-100): the fleet MQTT subscriber binds its lifecycle to
# the daphne process via the ASGI ``lifespan`` scope. ``ProtocolTypeRouter``
# raises on unknown scope types, so we intercept ``lifespan`` here and
# delegate to ``fleet.mqtt_transport.lifespan_handler``. All other scopes
# (``http`` / ``websocket``) pass through unchanged so the existing WS
# control-plane path keeps working during the grey-rollout window.
async def application(scope, receive, send):
    if scope.get("type") == "lifespan":
        await fleet_mqtt_lifespan(scope, receive, send)
        return
    await _protocol_router(scope, receive, send)
