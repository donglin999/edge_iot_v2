from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r"edges", views.EdgeNodeViewSet, basename="fleet-edge")
router.register(r"task-statuses", views.EdgeTaskStatusViewSet, basename="fleet-task-status")

urlpatterns = [
    path("", include(router.urls)),
]
