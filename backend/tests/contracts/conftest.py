"""Isolation guards for contract tests.

The executable contracts exercise model signals but must never publish into a
developer's Redis/demo stack. Returning no channel layer preserves the
production signal's documented best-effort/no-layer branch while keeping the
tests deterministic and entirely in-process.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_external_signal_broadcasts(monkeypatch) -> None:
    monkeypatch.setattr("acquisition.signals.get_channel_layer", lambda: None)
