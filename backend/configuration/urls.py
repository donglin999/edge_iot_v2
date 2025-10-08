"""API routing for configuration module."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"sites", views.SiteViewSet, basename="site")
router.register(r"devices", views.DeviceViewSet, basename="device")
router.register(r"channels", views.ChannelViewSet, basename="channel")
router.register(r"points", views.PointViewSet, basename="point")
router.register(r"tasks", views.AcqTaskViewSet, basename="task")
router.register(r"import-jobs", views.ImportJobViewSet, basename="import-job")
router.register(r"config-versions", views.ConfigVersionViewSet, basename="config-version")

urlpatterns = [
    path("", include(router.urls)),
]
