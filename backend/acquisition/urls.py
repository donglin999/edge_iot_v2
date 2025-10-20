"""API routing for acquisition module."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'sessions', views.AcquisitionSessionViewSet, basename='acquisition-session')
router.register(r'connection-tests', views.ConnectionTestViewSet, basename='connection-test')
router.register(r'storage-tests', views.StorageTestViewSet, basename='storage-test')

urlpatterns = [
    path('', include(router.urls)),
]
