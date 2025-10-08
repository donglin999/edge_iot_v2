"""Admin registrations for configuration models."""
from django.contrib import admin

from . import models


@admin.register(models.Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "created_at")
    search_fields = ("code", "name")


@admin.register(models.Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("code", "site", "protocol", "ip_address", "port")
    list_filter = ("protocol", "site")
    search_fields = ("code", "name", "ip_address")


@admin.register(models.Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("device", "number", "sampling_rate_hz")
    list_filter = ("device",)


@admin.register(models.Point)
class PointAdmin(admin.ModelAdmin):
    list_display = ("code", "device", "address", "to_kafka")
    list_filter = ("device", "to_kafka")
    search_fields = ("code", "address")


@admin.register(models.PointTemplate)
class PointTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "english_name", "unit", "data_type")
    search_fields = ("name", "english_name")


@admin.register(models.AcqTask)
class AcqTaskAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "schedule", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(models.ConfigVersion)
class ConfigVersionAdmin(admin.ModelAdmin):
    list_display = ("task", "version", "created_by", "created_at")
    list_filter = ("task",)


@admin.register(models.ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("source_name", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("source_name",)


@admin.register(models.WorkerEndpoint)
class WorkerEndpointAdmin(admin.ModelAdmin):
    list_display = ("identifier", "host", "status", "last_seen_at")
    list_filter = ("status",)
    search_fields = ("identifier", "host")


@admin.register(models.TaskRun)
class TaskRunAdmin(admin.ModelAdmin):
    list_display = ("task", "status", "started_at", "finished_at")
    list_filter = ("status", "task")


@admin.register(models.TaskPoint)
class TaskPointAdmin(admin.ModelAdmin):
    list_display = ("task", "point")
    list_filter = ("task",)
