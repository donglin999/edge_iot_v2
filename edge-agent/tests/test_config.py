"""EdgeConfig env parsing."""
from __future__ import annotations

import pytest

from edge_agent.config import ConfigError, EdgeConfig


def test_from_env_happy_path():
    cfg = EdgeConfig.from_env({
        "EDGE_ID": "e1",
        "EDGE_TOKEN": "tk",
        "CENTER_URL": "ws://c/ws/fleet/",
        "EDGE_LABELS": '{"site":"sh"}',
        "LOG_LEVEL": "debug",
    })
    assert cfg.edge_id == "e1"
    assert cfg.edge_token == "tk"
    assert cfg.center_url == "ws://c/ws/fleet/"
    assert cfg.labels == {"site": "sh"}
    assert cfg.log_level == "DEBUG"


def test_missing_required_raises():
    with pytest.raises(ConfigError) as exc:
        EdgeConfig.from_env({"EDGE_ID": "e1"})
    assert "EDGE_TOKEN" in str(exc.value)
    assert "CENTER_URL" in str(exc.value)


def test_labels_must_be_object():
    with pytest.raises(ConfigError):
        EdgeConfig.from_env({
            "EDGE_ID": "e1", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_LABELS": "[1,2,3]",
        })
    with pytest.raises(ConfigError):
        EdgeConfig.from_env({
            "EDGE_ID": "e1", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_LABELS": "not json",
        })


def test_labels_default_empty():
    cfg = EdgeConfig.from_env({
        "EDGE_ID": "e1", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
    })
    assert cfg.labels == {}
