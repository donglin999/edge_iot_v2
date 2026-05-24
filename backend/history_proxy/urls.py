"""URL config for the M6 history proxy (XIU-83)."""
from django.urls import path

from . import views

urlpatterns = [
    path("points", views.history_points, name="history-proxy-points"),
    # Trailing-slash variant so callers with DRF's "append slash" middleware
    # don't pay a redirect on every request.
    path("points/", views.history_points, name="history-proxy-points-slash"),
]
