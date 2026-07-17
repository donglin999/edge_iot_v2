"""API routing for configuration module."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"sites", views.SiteViewSet, basename="site")
router.register(r"scada-gateways", views.ScadaGatewayViewSet, basename="scada-gateway")
router.register(r"devices", views.DeviceViewSet, basename="device")
router.register(r"channels", views.ChannelViewSet, basename="channel")
router.register(r"points", views.PointViewSet, basename="point")
router.register(r"tasks", views.AcqTaskViewSet, basename="task")
router.register(r"import-jobs", views.ImportJobViewSet, basename="import-job")
router.register(r"versions", views.ConfigVersionViewSet, basename="config-version")

urlpatterns = [
    # Bare route — must precede router include so it isn't shadowed.
    path("export-excel/", views.ConfigExportView.as_view(), name="config-export-excel"),
    path("", include(router.urls)),
]
