"""Regression test for rank12: MQTT device identity must not collapse on the
default-empty ``mqtt_client_id``.

Before the fix, ``MQTTProtocol.IDENTITY_FIELDS`` included ``mqtt_client_id``,
whose ``FieldSpec`` default is ``""`` ("leave blank to auto-generate"). Two
devices on the same broker that both leave ClientID blank produced the same
identity tuple ``(ip, port, "")`` and were merged into a single device by the
importer (``configuration/services/importer.py`` groups rows by
``tuple(row_data.get(f) for f in klass.IDENTITY_FIELDS)``).

The fix swaps ``mqtt_client_id`` for ``mqtt_topics`` — the field that
actually distinguishes independent subscriptions on one broker — mirroring
the pattern already used by ``scada.py`` (``scada_product_key`` +
``scada_device_name``).
"""
from __future__ import annotations

from acquisition.protocols.mqtt import MQTTProtocol


def test_identity_fields_no_longer_include_client_id():
    assert "mqtt_client_id" not in MQTTProtocol.IDENTITY_FIELDS


def test_identity_fields_include_topics():
    assert MQTTProtocol.IDENTITY_FIELDS == ("source_ip", "source_port", "mqtt_topics")


def _identity(row: dict) -> tuple:
    return tuple(row.get(f) for f in MQTTProtocol.IDENTITY_FIELDS)


def test_two_devices_same_broker_blank_client_id_stay_distinct():
    """The bug scenario: same broker, both leave ClientID blank."""
    device_a = {
        "source_ip": "broker.local", "source_port": 1883,
        "mqtt_client_id": "", "mqtt_topics": "sensor/line1/+",
    }
    device_b = {
        "source_ip": "broker.local", "source_port": 1883,
        "mqtt_client_id": "", "mqtt_topics": "sensor/line2/+",
    }
    assert _identity(device_a) != _identity(device_b), (
        "two devices with different topics must not be de-duped just "
        "because they share a blank mqtt_client_id"
    )


def test_two_devices_same_broker_same_topics_now_merge_by_design():
    """Two rows with identical (ip, port, topics) are legitimately the same
    device — this is unchanged/expected merge behaviour, not a regression."""
    device_a = {
        "source_ip": "broker.local", "source_port": 1883,
        "mqtt_client_id": "", "mqtt_topics": "sensor/line1/+",
    }
    device_b = {
        "source_ip": "broker.local", "source_port": 1883,
        "mqtt_client_id": "edge-2",  # differing client_id no longer matters
        "mqtt_topics": "sensor/line1/+",
    }
    assert _identity(device_a) == _identity(device_b)
