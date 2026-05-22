"""Shared pytest fixtures for the edge-agent test-suite.

Tests that exercise the M2 config/ORM path need a working Django setup.
``edge_agent.django_setup.ensure_setup`` is a process-global once — the
first caller wins and every later call short-circuits. To make that
deterministic regardless of test collection order (XIU-61: a clean
checkout saw ``no such table`` because ``EdgeAgent`` bootstrapped the ORM
against the wrong DB before the fixture ran), this module:

1. Pins ``EDGE_STATE_DB`` / ``DJANGO_LOG_DIR`` to a session temp dir at
   import time — before any test or ``EdgeAgent`` instance can call
   ``ensure_setup`` — so every caller converges on the same temp DB.
2. Eagerly runs ``ensure_setup`` here, so migrations are applied once,
   up front, in a plain sync context.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# The edge-agent imports ``backend.acquisition`` at runtime. Put the repo's
# ``backend/`` dir on sys.path so ``configuration`` / ``acquisition`` resolve.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
os.environ.setdefault("BACKEND_DIR", str(_BACKEND_DIR))

# Pin the edge state DB + log dir to a session temp dir *before* anything
# can call ensure_setup(). Every ensure_setup() call — fixture or EdgeAgent
# — then resolves to the same migrated temp DB.
_TMP_DIR = tempfile.mkdtemp(prefix="edge-agent-test-")
_TMP_DB = os.path.join(_TMP_DIR, "edge_state.db")
os.environ["EDGE_STATE_DB"] = _TMP_DB
os.environ["DJANGO_LOG_DIR"] = os.path.join(_TMP_DIR, "logs")

# Eagerly bootstrap Django + migrations now, in a plain sync context, so the
# tables exist before any test runs and the once-flag short-circuits cleanly.
from edge_agent.django_setup import ensure_setup  # noqa: E402

ensure_setup(state_db=_TMP_DB, run_migrations=True)


@pytest.fixture(scope="session")
def django_edge():
    """The edge-agent Django runtime, booted against a temp SQLite DB.

    ``ensure_setup`` already ran at import; this fixture just hands tests
    the DB path. Kept as a fixture so tests express the dependency
    explicitly.
    """
    return _TMP_DB
