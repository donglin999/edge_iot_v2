from django.contrib import admin

from .models import EdgeAssignment, EdgeNode, EdgeTaskStatus


@admin.register(EdgeNode)
class EdgeNodeAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "version", "last_seen", "updated_at")
    list_filter = ("status",)
    search_fields = ("name",)
    readonly_fields = ("token_hash", "created_at", "updated_at", "last_seen")


@admin.register(EdgeAssignment)
class EdgeAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "edge",
        "task",
        "desired_state",
        "config_version",
        "last_applied_version",
        "applied_at",
    )
    list_filter = ("desired_state",)
    search_fields = ("edge__name", "task__code")
    readonly_fields = ("created_at", "updated_at")


@admin.register(EdgeTaskStatus)
class EdgeTaskStatusAdmin(admin.ModelAdmin):
    list_display = ("edge", "task", "state", "last_reported_at")
    list_filter = ("state",)
    search_fields = ("edge__name", "task__code")
    readonly_fields = ("created_at", "updated_at", "last_reported_at")
