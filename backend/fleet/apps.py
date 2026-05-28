from django.apps import AppConfig


class FleetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fleet"
    verbose_name = "Edge fleet"

    def ready(self) -> None:
        # Connect the M4 alarm-rule → edge re-sync signal handlers.
        from . import signals  # noqa: F401
