"""Bootstrap Django so the edge-agent can ``import backend.acquisition`` cleanly.

The edge-agent does *not* run a Django HTTP server — it just needs the ORM and
the acquisition pipeline machinery. We set ``DJANGO_SETTINGS_MODULE`` (with a
sensible default that points at the bundled center settings), make sure the
``backend`` directory is on ``sys.path``, point the SQLite database at a
per-edge ``edge_state.db`` (so the edge does not poke the center's DB), and
then call ``django.setup()`` once.

After ``ensure_setup()`` returns, ``configuration.models``,
``acquisition.models`` and ``acquisition.services.acquisition_service`` are
all importable. The first call also runs ``migrate --run-syncdb`` so a fresh
edge bring-up creates the tables it needs without an extra manual step.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_BACKEND_DIR = "/opt/backend"
_DEFAULT_SETTINGS_MODULE = "control_plane.settings"
_DEFAULT_STATE_DB = "/var/lib/edge-agent/edge_state.db"

_setup_lock = threading.Lock()
_setup_done = False


def _ensure_state_db_path(default_state_db: str) -> str:
    """Pick the SQLite path the edge will use and make sure the dir exists."""
    state_db = os.environ.get("EDGE_STATE_DB", default_state_db)
    Path(state_db).parent.mkdir(parents=True, exist_ok=True)
    return state_db


def _ensure_backend_on_path() -> None:
    backend_dir = os.environ.get("BACKEND_DIR", _DEFAULT_BACKEND_DIR)
    if backend_dir and backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)


def _ensure_writable_log_dir(state_db: str) -> None:
    """Point the center's ``LOGGING`` at a writable directory.

    The center settings writes ``application.log`` into ``DJANGO_LOG_DIR``
    (default ``BASE_DIR/logs``). On the edge ``BASE_DIR`` is the read-only
    ``/opt/backend`` mount, so we redirect the log dir next to the
    edge-side state DB — the same writable volume — unless the operator
    already set ``DJANGO_LOG_DIR`` explicitly. The center settings also
    falls back to console-only logging if the dir is unwritable, so this
    is belt-and-suspenders: it just gives the edge real file logs.
    """
    if os.environ.get("DJANGO_LOG_DIR"):
        return
    log_dir = Path(state_db).parent / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DJANGO_LOG_DIR"] = str(log_dir)
    except OSError:
        # Leave DJANGO_LOG_DIR unset — settings will fall back to console.
        logger.warning("edge-agent: could not create log dir %s", log_dir)


def ensure_setup(
    *,
    settings_module: str | None = None,
    state_db: str | None = None,
    run_migrations: bool = True,
) -> str:
    """Idempotent Django bootstrap for the edge-agent process.

    Returns the SQLite path actually being used. Safe to call from
    anywhere — internally guarded by a lock + once-flag.
    """
    global _setup_done

    with _setup_lock:
        if _setup_done:
            return os.environ.get("DJANGO_DB_NAME", "")

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module or _DEFAULT_SETTINGS_MODULE)
        # The center's settings reads DEBUG / SECRET_KEY / etc. via django-environ
        # with sensible defaults. For the edge we override the DB path only;
        # everything else can use those defaults (DEBUG=True, dev secret key).
        chosen_db = _ensure_state_db_path(state_db or _DEFAULT_STATE_DB)
        os.environ["DJANGO_DB_NAME"] = chosen_db
        # Redirect the center's file logging to a writable dir BEFORE
        # django.setup() — the center code is mounted read-only on the edge,
        # so logging into BASE_DIR/logs would abort django.setup() (XIU-61
        # defect B). Must happen before the settings module is imported.
        _ensure_writable_log_dir(chosen_db)
        # The center settings parses ALLOWED_HOSTS / SECRET_KEY at import time;
        # nothing else is needed for ORM-only usage. Leave whatever the
        # operator set in the env in place.

        _ensure_backend_on_path()

        import django  # imported lazily so module import is cheap in unit tests

        django.setup()
        _setup_done = True

        logger.info(
            "edge-agent Django ready: settings=%s db=%s",
            os.environ["DJANGO_SETTINGS_MODULE"], chosen_db,
        )

        if run_migrations:
            _run_migrations()

        return chosen_db


def _run_migrations() -> None:
    """Apply outstanding migrations against the edge-side SQLite DB.

    We always run with ``--run-syncdb`` so that even on a brand-new edge
    (no migrations folder seeded) the unmanaged third-party tables are
    created. Errors are logged but never raise — bringing up the WS
    must not be blocked by a migration glitch on a fresh box; the
    operator can investigate from the logs.
    """
    try:
        from django.core.management import call_command

        call_command("migrate", "--run-syncdb", "--noinput", verbosity=0)
        logger.info("edge-agent migrations applied")
    except Exception:  # noqa: BLE001
        logger.exception("edge-agent migrate failed — continuing without")
