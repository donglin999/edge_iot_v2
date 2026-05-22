from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"edges", views.EdgeNodeViewSet, basename="fleet-edge")
router.register(r"task-statuses", views.EdgeTaskStatusViewSet, basename="fleet-task-status")
router.register(r"lifecycle-events", views.EdgeLifecycleEventViewSet, basename="fleet-lifecycle-event")
router.register(r"samples", views.EdgeSampleViewSet, basename="fleet-sample")

urlpatterns = [
    path("", include(router.urls)),
]
