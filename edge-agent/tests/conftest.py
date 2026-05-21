"""Shared pytest fixtures for the edge-agent test-suite.

Tests that exercise the M2 config/ORM path need a working Django setup.
``django_edge`` boots Django once per session against a throwaway SQLite
file so ``edge_agent.state.persist_to_orm`` and ``edge_agent.runner`` can
use the real ORM without touching the developer's database.
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


@pytest.fixture(scope="session")
def django_edge():
    """Boot the edge-agent's Django runtime against a temp SQLite DB."""
    tmp_dir = tempfile.mkdtemp(prefix="edge-agent-test-")
    db_path = os.path.join(tmp_dir, "edge_state.db")
    os.environ["EDGE_STATE_DB"] = db_path

    from edge_agent.django_setup import ensure_setup

    ensure_setup(state_db=db_path, run_migrations=True)
    yield db_path
