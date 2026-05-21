from django.contrib import admin

from .models import EdgeNode


@admin.register(EdgeNode)
class EdgeNodeAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "version", "last_seen", "updated_at")
    list_filter = ("status",)
    search_fields = ("name",)
    readonly_fields = ("token_hash", "created_at", "updated_at", "last_seen")
