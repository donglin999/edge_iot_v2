"""control_plane URL Configuration."""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="api-redoc"),
    path("api/auth/", include("rest_framework.urls")),
    path("api/config/", include("configuration.urls")),
]
