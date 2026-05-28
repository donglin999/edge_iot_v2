import logging
import os
import sys
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class FleetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fleet"
    verbose_name = "Edge fleet"

    # Module-level guard: ``ready()`` is invoked for every management command
    # / autoreload child / celery worker, and we only want the MQTT
    # subscriber thread once — in the daphne ASGI process. A class attribute
    # is fine because each Python process loads its own AppConfig instance.
    _mqtt_thread: "threading.Thread | None" = None

    def ready(self) -> None:
        # Connect the M4 alarm-rule → edge re-sync signal handlers.
        from . import signals  # noqa: F401

        self._maybe_start_mqtt_subscriber()

    # ---- MQTT subscriber (XIU-100 Phase 2 P1) -----------------------------

    @classmethod
    def _maybe_start_mqtt_subscriber(cls) -> None:
        """Spawn the asyncio MQTT subscriber in a daemon thread.

        Daphne 4.1.2 does not drive ASGI lifespan, so an ``application``-level
        wrapper would never see ``lifespan.startup``. Starting the subscriber
        from :meth:`ready` instead works for every ASGI worker — and the
        ``daphne``-in-argv guard keeps the connection out of one-shot
        commands (``manage.py migrate`` / ``check`` / shell) and the celery
        workers that share the same Django settings.
        """
        from django.conf import settings

        if not getattr(settings, "FLEET_MQTT_ENABLED", False):
            return
        if cls._mqtt_thread is not None:
            return
        if not cls._running_under_daphne():
            return

        # Import lazily so a host without aiomqtt installed (the legacy
        # monolith image, for instance) keeps importing fleet.apps fine.
        try:
            from .mqtt_transport import MqttTransportConfig
        except Exception:  # noqa: BLE001
            logger.exception("fleet: mqtt_transport import failed — subscriber disabled")
            return

        config = MqttTransportConfig(
            host=getattr(settings, "FLEET_MQTT_HOST", "127.0.0.1"),
            port=int(getattr(settings, "FLEET_MQTT_PORT", 1883)),
            client_id=getattr(settings, "FLEET_MQTT_CLIENT_ID", "center-fleet-subscriber"),
            username=getattr(settings, "FLEET_MQTT_USERNAME", "") or None,
            password=getattr(settings, "FLEET_MQTT_PASSWORD", "") or None,
        )
        cls._mqtt_thread = threading.Thread(
            target=cls._run_subscriber_loop,
            args=(config,),
            name="fleet-mqtt-subscriber",
            daemon=True,
        )
        cls._mqtt_thread.start()
        logger.info(
            "fleet: spawned MQTT subscriber thread → %s:%d", config.host, config.port,
        )

    @staticmethod
    def _running_under_daphne() -> bool:
        # Daphne sets argv[0] to the daphne entrypoint. Pre-empt accidental
        # starts in ``manage.py migrate`` etc. by checking both argv[0] and
        # the rest of argv (covers ``python -m daphne`` invocations).
        argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
        if argv0 == "daphne" or argv0.endswith("/daphne"):
            return True
        return any("daphne" in str(a).lower() for a in sys.argv[1:])

    @staticmethod
    def _run_subscriber_loop(config) -> None:
        """Thread entrypoint — owns a private asyncio loop for the subscriber."""
        import asyncio

        from .mqtt_transport import run_subscriber

        try:
            asyncio.run(run_subscriber(config))
        except Exception:  # noqa: BLE001
            logger.exception("fleet: mqtt subscriber thread crashed")
