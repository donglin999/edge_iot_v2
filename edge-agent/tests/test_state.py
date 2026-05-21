"""Tests for the edge-side config cache (XIU-59 / M2).

Covers:
- EdgeStateStore KV round-trip (survives process restart)
- parse_apply_config validation
- persist_to_orm reconciling apply_config into the local Django ORM
"""
from __future__ import annotations

import pytest

from edge_agent.state import (
    CachedConfig,
    EdgeStateStore,
    apply_frame,
    parse_apply_config,
    persist_to_orm,
)


def _sample_frame(version: int = 1) -> dict:
    return {
        "v": "0.2",
        "type": "apply_config",
        "version": version,
        "tasks": [
            {
                "id": 12,
                "code": "modbus-line-1",
                "name": "Modbus Line 1",
                "sample_rate_hz": 1.0,
                "is_active": True,
                "point_ids": [101, 102],
            }
        ],
        "devices": [
            {
                "id": 5,
                "code": "modbus-tcp-1",
                "name": "Modbus TCP 1",
                "protocol": "modbus_tcp",
                "ip_address": "mock-modbus-edge",
                "port": 5020,
                "metadata": {},
            }
        ],
        "points": [
            {
                "id": 101,
                "device_id": 5,
                "code": "holding_0",
                "address": "40001",
                "sample_rate_hz": 1.0,
                "extra": {"register_type": "holding"},
                "template": {
                    "name": "H0", "english_name": "h0", "unit": "",
                    "data_type": "uint16", "coefficient": 1.0, "precision": 0,
                },
            },
            {
                "id": 102,
                "device_id": 5,
                "code": "holding_1",
                "address": "40002",
                "sample_rate_hz": 1.0,
                "extra": {"register_type": "holding"},
                "template": {
                    "name": "H1", "english_name": "h1", "unit": "",
                    "data_type": "uint16", "coefficient": 1.0, "precision": 0,
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# EdgeStateStore
# ---------------------------------------------------------------------------


def test_state_store_persists_frame_and_version(tmp_path):
    store = EdgeStateStore(tmp_path / "state.db")
    assert store.get_last_version() is None
    assert store.load_last_frame() is None

    frame = _sample_frame(version=7)
    store.save_frame(frame)

    assert store.get_last_version() == 7
    loaded = store.load_last_frame()
    assert loaded["version"] == 7
    assert len(loaded["tasks"]) == 1


def test_state_store_survives_reopen(tmp_path):
    db = tmp_path / "state.db"
    EdgeStateStore(db).save_frame(_sample_frame(version=3))

    # Simulate an edge-agent restart: fresh store object, same file.
    reopened = EdgeStateStore(db)
    assert reopened.get_last_version() == 3
    assert reopened.load_last_frame()["version"] == 3


def test_state_store_overwrites_on_newer_frame(tmp_path):
    store = EdgeStateStore(tmp_path / "state.db")
    store.save_frame(_sample_frame(version=1))
    store.save_frame(_sample_frame(version=2))
    assert store.get_last_version() == 2


# ---------------------------------------------------------------------------
# parse_apply_config
# ---------------------------------------------------------------------------


def test_parse_apply_config_happy_path():
    cached = parse_apply_config(_sample_frame(version=4))
    assert isinstance(cached, CachedConfig)
    assert cached.version == 4
    assert cached.task_ids == [12]
    assert len(cached.devices) == 1
    assert len(cached.points) == 2


def test_parse_apply_config_rejects_missing_version():
    bad = _sample_frame()
    del bad["version"]
    with pytest.raises(ValueError):
        parse_apply_config(bad)


def test_parse_apply_config_rejects_non_list_tasks():
    bad = _sample_frame()
    bad["tasks"] = {"not": "a list"}
    with pytest.raises(ValueError):
        parse_apply_config(bad)


# ---------------------------------------------------------------------------
# persist_to_orm (needs Django)
# ---------------------------------------------------------------------------


def test_persist_to_orm_creates_rows(django_edge):
    from configuration.models import AcqTask, Device, Point, TaskPoint

    # Clean slate so the assertions are deterministic across test ordering.
    AcqTask.objects.all().delete()
    Device.objects.all().delete()
    Point.objects.all().delete()

    cached = parse_apply_config(_sample_frame(version=1))
    persist_to_orm(cached)

    task = AcqTask.objects.get(pk=12)
    assert task.code == "modbus-line-1"
    assert Device.objects.filter(pk=5).exists()
    assert Point.objects.filter(pk__in=[101, 102]).count() == 2
    assert TaskPoint.objects.filter(task=task).count() == 2
    assert {p.code for p in task.points.all()} == {"holding_0", "holding_1"}


def test_persist_to_orm_culls_removed_tasks(django_edge):
    from configuration.models import AcqTask, Device, Point

    AcqTask.objects.all().delete()
    Device.objects.all().delete()
    Point.objects.all().delete()

    # First snapshot has task 12.
    persist_to_orm(parse_apply_config(_sample_frame(version=1)))
    assert AcqTask.objects.filter(pk=12).exists()

    # Second snapshot drops all tasks — task 12 must be culled.
    empty = _sample_frame(version=2)
    empty["tasks"] = []
    empty["points"] = []
    empty["devices"] = []
    persist_to_orm(parse_apply_config(empty))
    assert not AcqTask.objects.filter(pk=12).exists()


def test_apply_frame_persists_and_caches(django_edge, tmp_path):
    from configuration.models import AcqTask, Device, Point

    AcqTask.objects.all().delete()
    Device.objects.all().delete()
    Point.objects.all().delete()

    store = EdgeStateStore(tmp_path / "state.db")
    cached = apply_frame(store, _sample_frame(version=9))

    assert cached.version == 9
    assert store.get_last_version() == 9
    assert AcqTask.objects.filter(pk=12).exists()
