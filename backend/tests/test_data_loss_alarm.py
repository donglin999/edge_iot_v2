"""Data-loss visibility: MQTT queue overflow must be counted, surfaced, alarmed.

实测背景(2026-07 压测):往 scada mock 通路灌 2240 msg/s,10 秒丢了 11492 条,
而会话照样 running、设备照样在线、UI 毫无异样 —— 丢弃只出现在 worker 日志里。
这组测试钉死修复后的契约:

* 协议层:``mqtt_queue_size`` 可配;队列满 → ``dropped_messages`` 自增(不再只是
  一行日志)。
* worker 层:丢弃数进入健康记录(``dropped_messages``,跨重连累计),丢弃增长拉
  ``data_loss`` 系统告警(dedup 幂等),静默 ``DROP_ALARM_CLEAR_WINDOW_S`` 后自动
  清除并重新武装。
* 拉模式协议没有 ``dropped_messages`` 属性 → 全程零开销短路。
"""
import threading
import time
from types import SimpleNamespace

import pytest

from acquisition import models as acq_models
from acquisition.services import reporting
from acquisition.protocols.mqtt import MQTTProtocol
from acquisition.services import pipeline as pipeline_mod
from acquisition.services.pipeline import ReadWorker


# ---------------------------------------------------------------------------
# protocol level
# ---------------------------------------------------------------------------


class _Msg(SimpleNamespace):
    """paho message stand-in: only the attrs _on_message touches."""


def _mqtt(config_extra=None):
    cfg = {"source_ip": "127.0.0.1", "source_port": 1883, "mqtt_topics": "t/#"}
    cfg.update(config_extra or {})
    return MQTTProtocol(cfg)


def _push(proto, n):
    for i in range(n):
        proto._on_message(None, None, _Msg(topic="t/x", payload=b"1", qos=0))


class TestMqttQueueOverflowCounter:
    def test_queue_size_is_configurable(self):
        proto = _mqtt({"mqtt_queue_size": 3})
        assert proto.data_queue.maxsize == 3

    def test_queue_size_defaults_to_1000_and_rejects_garbage(self):
        assert _mqtt().data_queue.maxsize == 1000
        assert _mqtt({"mqtt_queue_size": "abc"}).data_queue.maxsize == 1000
        assert _mqtt({"mqtt_queue_size": -5}).data_queue.maxsize == 1000

    def test_overflow_increments_dropped_messages(self):
        proto = _mqtt({"mqtt_queue_size": 2})
        _push(proto, 5)
        assert proto.data_queue.qsize() == 2
        assert proto.dropped_messages == 3

    def test_no_overflow_no_drops(self):
        proto = _mqtt({"mqtt_queue_size": 10})
        _push(proto, 5)
        assert proto.dropped_messages == 0


# ---------------------------------------------------------------------------
# worker level
# ---------------------------------------------------------------------------


class _PushProto:
    """Connected push-protocol stub with a controllable drop counter."""

    def __init__(self) -> None:
        self.is_connected = True
        self.dropped_messages = 0

    def read_batch(self, group):  # pragma: no cover - empty read plan
        return []

    def disconnect(self) -> None:
        self.is_connected = False


class _PullProto:
    """Pull-protocol stub: no ``dropped_messages`` attribute at all."""

    def __init__(self) -> None:
        self.is_connected = True

    def read_batch(self, group):  # pragma: no cover
        return []

    def disconnect(self) -> None:
        self.is_connected = False


def _make_worker(device_code="DEV-DROP"):
    device = SimpleNamespace(
        id=1,
        code=device_code,
        protocol="generic",  # non-modbus -> empty read plan
        metadata={},
        ip_address="127.0.0.1",
        port=502,
    )
    return ReadWorker(
        device=device,
        points=[],
        sinks=[],
        sample_rate_hz=100.0,
        shutdown_event=threading.Event(),
        health_dict={},
        session=None,
    )


def _firing(device_code):
    return acq_models.Alarm.objects.filter(
        category="data_loss",
        dedup_key=f"data_loss:{device_code}",
        status=acq_models.Alarm.STATUS_FIRING,
    )


@pytest.mark.django_db
class TestWorkerDropTracking:
    def test_no_drops_is_a_cheap_noop(self):
        worker = _make_worker()
        worker.protocol = _PushProto()
        worker._track_dropped_messages()
        assert "dropped_messages" not in worker.health
        assert _firing("DEV-DROP").count() == 0

    def test_pull_protocol_without_attribute_is_a_noop(self):
        worker = _make_worker()
        worker.protocol = _PullProto()
        worker._track_dropped_messages()
        assert "dropped_messages" not in worker.health
        assert _firing("DEV-DROP").count() == 0

    def test_drop_increase_publishes_health_and_raises_alarm(self):
        worker = _make_worker()
        proto = _PushProto()
        worker.protocol = proto

        proto.dropped_messages = 42
        worker._track_dropped_messages()

        assert worker.health["dropped_messages"] == 42
        alarms = list(_firing("DEV-DROP"))
        assert len(alarms) == 1
        assert "42" in alarms[0].message
        assert alarms[0].value["dropped_total"] == 42

    def test_further_increases_update_health_without_duplicate_alarm(self):
        worker = _make_worker()
        proto = _PushProto()
        worker.protocol = proto

        proto.dropped_messages = 10
        worker._track_dropped_messages()
        proto.dropped_messages = 25
        worker._track_dropped_messages()

        assert worker.health["dropped_messages"] == 25
        assert _firing("DEV-DROP").count() == 1

    def test_alarm_clears_after_quiet_window_and_rearms(self):
        worker = _make_worker()
        proto = _PushProto()
        worker.protocol = proto

        proto.dropped_messages = 10
        worker._track_dropped_messages()
        assert _firing("DEV-DROP").count() == 1

        # 静默窗口耗尽 → 清除。把"最后一次增长"拨回过去,避免真等 60s。
        worker._last_drop_increase_at = time.time() - (
            ReadWorker.DROP_ALARM_CLEAR_WINDOW_S + 1
        )
        worker._track_dropped_messages()
        assert _firing("DEV-DROP").count() == 0

        # 再次丢弃 → 重新武装,拉出新告警。
        proto.dropped_messages = 11
        worker._track_dropped_messages()
        assert _firing("DEV-DROP").count() == 1

    def test_counter_survives_protocol_replacement(self):
        """重连会换协议实例、实例计数归零 —— worker 折叠出跨实例累计值。"""
        worker = _make_worker()
        old = _PushProto()
        old.dropped_messages = 30
        worker.protocol = old

        worker._track_dropped_messages()
        assert worker.health["dropped_messages"] == 30

        # 模拟 _record_failure 超时路径的折叠 + 新实例上再丢 5 条。
        worker._dropped_base += old.dropped_messages
        fresh = _PushProto()
        fresh.dropped_messages = 5
        worker.protocol = fresh

        worker._track_dropped_messages()
        assert worker.health["dropped_messages"] == 35

    def test_failed_alarm_transitions_are_retried(self, monkeypatch):
        worker = _make_worker()
        proto = _PushProto()
        worker.protocol = proto
        raises = iter((None, object()))
        clears = iter((None, 1))
        monotonic_now = 100.0

        monkeypatch.setattr(
            reporting, "raise_system_alarm", lambda **_kwargs: next(raises),
        )
        monkeypatch.setattr(
            reporting, "clear_system_alarm", lambda _dedup_key: next(clears),
        )
        monkeypatch.setattr(
            pipeline_mod.time, "monotonic", lambda: monotonic_now,
        )

        proto.dropped_messages = 1
        worker._track_dropped_messages()
        assert worker._drop_alarm_raised is False
        worker._track_dropped_messages()
        assert worker._drop_alarm_raised is False

        monotonic_now = 101.0
        worker._track_dropped_messages()
        assert worker._drop_alarm_raised is True

        worker._last_drop_increase_at = time.time() - (
            ReadWorker.DROP_ALARM_CLEAR_WINDOW_S + 1
        )
        worker._track_dropped_messages()
        assert worker._drop_alarm_raised is True
        worker._track_dropped_messages()
        assert worker._drop_alarm_raised is True

        monotonic_now = 102.0
        worker._track_dropped_messages()
        assert worker._drop_alarm_raised is False


# ---------------------------------------------------------------------------
# push mode: scada/mqtt 无采集频率概念
# ---------------------------------------------------------------------------


class TestPushModeWorker:
    """推模式协议(mqtt/scada)事件驱动:循环不睡,节奏由队列阻塞 get 提供。"""

    def _worker(self, protocol):
        device = SimpleNamespace(
            id=1, code="DEV-P", protocol=protocol, metadata={},
            ip_address="127.0.0.1", port=502,
        )
        return ReadWorker(
            device=device, points=[], sinks=[], sample_rate_hz=1.0,
            shutdown_event=threading.Event(), health_dict={}, session=None,
        )

    def test_scada_and_mqtt_are_push_mode_with_zero_cycle(self):
        for proto in ("scada", "mqtt"):
            w = self._worker(proto)
            assert w.push_mode is True
            assert w.cycle_interval == 0.0
            # 超时不与周期挂钩(否则 min(timeout, 0) 会截断队列阻塞等待)
            assert w._auto_timeout == 5.0

    def test_pull_protocols_keep_rate_based_cycle(self):
        for proto in ("modbus_tcp", "siemens_s7", "opcua"):
            w = self._worker(proto)
            assert w.push_mode is False
            assert w.cycle_interval == 1.0
