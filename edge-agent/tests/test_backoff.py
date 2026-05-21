"""Reconnect backoff — verifies the exact sequence the protocol doc promises."""
from __future__ import annotations

import pytest

from edge_agent.backoff import ExponentialBackoff


def test_sequence_matches_protocol_spec():
    """Spec (docs/distributed/protocol.md): 1 → 2 → 4 → 8 → 16 → 30 → 30 …"""
    b = ExponentialBackoff(initial=1.0, factor=2.0, cap=30.0)

    sequence = [b.next() for _ in range(8)]

    assert sequence == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_reset_returns_to_initial():
    b = ExponentialBackoff(initial=1.0, factor=2.0, cap=30.0)
    for _ in range(5):
        b.next()

    b.reset()

    assert b.next() == 1.0


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=0.0)
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=1.0, factor=1.0)
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=10.0, cap=1.0)
