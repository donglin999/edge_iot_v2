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
    # M6: history_url is auto-merged into labels so the center can dial
    # the edge's history HTTP endpoint without an out-of-band config.
    assert cfg.labels["site"] == "sh"
    assert cfg.labels["history_url"] == "http://e1:18086"
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
    # M6 auto-injects history_url; otherwise no labels.
    assert cfg.labels == {"history_url": "http://e1:18086"}
