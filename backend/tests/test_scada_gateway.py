"""Tests for the SCADA gateway: model, config assembly helper, CRUD, provision.

The gateway normalises the MQTT connection block that every 中山小家电 SCADA
device used to repeat in its own ``Device.metadata``. Two things must hold:

* the protocol still receives the exact same ``device_config`` keys it did
  before (it is unaware gateways exist), and
* devices *without* a gateway keep the byte-for-byte config they had before
  this refactor.
"""
import threading

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from acquisition import models as acq_models
from acquisition.services import pipeline as pipeline_mod
from acquisition.services.device_config import build_device_config
from acquisition.services.pipeline import ReadWorker
from configuration import models as config_models


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def site(db):
    return config_models.Site.objects.create(code="zs", name="中山")


@pytest.fixture
def gateway(db):
    return config_models.ScadaGateway.objects.create(
        code="gw1",
        name="中山 SCADA 网关",
        source_ip="10.134.14.147",
        source_port=8883,
        mqtt_use_tls=True,
        mqtt_username="ZYY_XJDZS",
        mqtt_password="s3cret",
        mqtt_qos=1,
        mqtt_client_id="edge-1",
        mqtt_read_timeout=7.5,
        product_key="123daffb91264286adcdf3bfe55194c7",
    )


def _make_device(site, gateway=None, **kwargs):
    defaults = {
        "site": site,
        "code": "scada-gw1-A0201010001150403",
        "name": "注塑机1",
        "protocol": "scada",
        "ip_address": "10.134.14.147",
        "port": 8883,
        "metadata": {"scada_device_name": "A0201010001150403"},
        "gateway": gateway,
    }
    defaults.update(kwargs)
    return config_models.Device.objects.create(**defaults)


# ---------------------------------------------------------------------------
# build_device_config
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBuildDeviceConfig:
    def test_gateway_fields_land_on_protocol_keys(self, site, gateway):
        """A gateway-backed device gets the full SCADAProtocol key set."""
        device = _make_device(site, gateway=gateway)

        cfg = build_device_config(device)

        assert cfg == {
            "source_ip": "10.134.14.147",
            "source_port": 8883,
            "protocol_type": "scada",
            "mqtt_use_tls": True,
            "mqtt_username": "ZYY_XJDZS",
            "mqtt_password": "s3cret",
            "mqtt_qos": 1,
            "mqtt_client_id": "edge-1",
            "mqtt_read_timeout": 7.5,
            "scada_product_key": "123daffb91264286adcdf3bfe55194c7",
            "scada_topic_template": config_models.ScadaGateway.DEFAULT_TOPIC_TEMPLATE,
            # the one key that legitimately varies per device
            "scada_device_name": "A0201010001150403",
        }

    def test_scada_protocol_accepts_the_assembled_config(self, site, gateway):
        """End-to-end: the dict we build actually drives the real protocol.

        Guards the model-field -> protocol-key translation: a typo in
        ``_gateway_overlay`` would leave the topic un-substituted here.
        """
        from acquisition.protocols.scada import SCADAProtocol

        device = _make_device(site, gateway=gateway)
        proto = SCADAProtocol(build_device_config(device))

        assert proto.product_key == "123daffb91264286adcdf3bfe55194c7"
        assert proto.device_name == "A0201010001150403"
        assert proto.broker_ip == "10.134.14.147"
        assert proto.broker_port == 8883
        assert proto.subscribe_topic == (
            "/sys/123daffb91264286adcdf3bfe55194c7/device/"
            "A0201010001150403/thing/property/+/post"
        )

    def test_non_gateway_device_config_is_unchanged(self, site):
        """Devices without a gateway keep the exact pre-refactor shape."""
        device = config_models.Device.objects.create(
            site=site,
            code="PLC-1",
            name="plc",
            protocol="modbus_tcp",
            ip_address="192.168.1.10",
            port=502,
            metadata={"slave_id": 3, "timeout": 9.0},
        )

        cfg = build_device_config(device)

        # This literal is the historical open-coded dict, verbatim.
        assert cfg == {
            "source_ip": "192.168.1.10",
            "source_port": 502,
            "protocol_type": "modbus_tcp",
            "slave_id": 3,
            "timeout": 9.0,
        }

    def test_device_metadata_overrides_a_gateway_value(self, site, gateway):
        """A single device may carry an exception to the shared config."""
        device = _make_device(
            site,
            gateway=gateway,
            metadata={
                "scada_device_name": "A0201010001150403",
                "mqtt_qos": 2,
                "source_ip": "10.9.9.9",
            },
        )

        cfg = build_device_config(device)

        assert cfg["mqtt_qos"] == 2
        assert cfg["source_ip"] == "10.9.9.9"
        # ...while everything else still comes from the gateway.
        assert cfg["mqtt_username"] == "ZYY_XJDZS"

    def test_overrides_beat_device_metadata(self, site):
        """Caller overrides (pipeline timeout) win over user-supplied values."""
        device = config_models.Device.objects.create(
            site=site, code="PLC-2", name="plc", protocol="modbus_tcp",
            ip_address="192.168.1.11", port=502, metadata={"timeout": 99.0},
        )

        cfg = build_device_config(device, overrides={"timeout": 1.5})

        assert cfg["timeout"] == 1.5

    def test_empty_metadata_is_tolerated(self, site, gateway):
        device = _make_device(site, gateway=gateway, metadata={})

        cfg = build_device_config(device)

        assert cfg["scada_product_key"] == gateway.product_key
        assert "scada_device_name" not in cfg


# ---------------------------------------------------------------------------
# Continuous pipeline path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPipelinePicksUpGateway:
    def test_read_worker_connect_uses_gateway_config(self, site, gateway, monkeypatch):
        """The continuous pipeline's _connect must see the gateway overlay.

        This is the path that actually collects in production — a helper that
        only the one-shot path called would leave gateway devices dead here.
        """
        device = _make_device(site, gateway=gateway)
        session = acq_models.AcquisitionSession.objects.create(
            task=config_models.AcqTask.objects.create(code="t1", name="t1"),
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
        )

        captured = {}

        class _StubProto:
            def connect(self):
                return True

        monkeypatch.setattr(
            pipeline_mod.ProtocolRegistry, "create",
            classmethod(lambda cls, protocol_type, cfg: captured.update(cfg) or _StubProto()),
        )

        worker = ReadWorker(
            device=device,
            points=[],
            sinks=[],
            sample_rate_hz=1.0,
            shutdown_event=threading.Event(),
            health_dict={},
            session=session,
        )
        assert worker._connect() is True

        assert captured["scada_product_key"] == "123daffb91264286adcdf3bfe55194c7"
        assert captured["scada_device_name"] == "A0201010001150403"
        assert captured["mqtt_username"] == "ZYY_XJDZS"
        assert captured["mqtt_read_timeout"] == 7.5
        # the pipeline's auto-derived timeout still wins
        assert captured["timeout"] == worker._auto_timeout


# ---------------------------------------------------------------------------
# CRUD API
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestScadaGatewayCRUD:
    def test_create_gateway(self, api_client):
        response = api_client.post("/api/config/scada-gateways/", {
            "code": "gw-new",
            "name": "新网关",
            "source_ip": "10.0.0.1",
            "source_port": 8883,
            "product_key": "pk123",
        }, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["code"] == "gw-new"
        # defaults applied
        assert response.data["mqtt_use_tls"] is True
        assert response.data["mqtt_qos"] == 0
        assert response.data["mqtt_read_timeout"] == 5.0
        assert response.data["topic_template"] == (
            config_models.ScadaGateway.DEFAULT_TOPIC_TEMPLATE
        )
        assert response.data["device_count"] == 0

    def test_serializer_fields_match_the_frozen_contract(self, api_client, gateway):
        response = api_client.get(f"/api/config/scada-gateways/{gateway.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert set(response.data.keys()) == {
            "id", "code", "name", "source_ip", "source_port", "mqtt_use_tls",
            "mqtt_username", "mqtt_password", "mqtt_qos", "mqtt_client_id",
            "mqtt_read_timeout", "product_key", "topic_template",
            "device_count", "created_at", "updated_at",
        }

    def test_list_gateways(self, api_client, gateway):
        response = api_client.get("/api/config/scada-gateways/")

        assert response.status_code == status.HTTP_200_OK
        # paginated envelope, same as every other config list endpoint
        assert set(response.data) == {"count", "next", "previous", "results"}
        codes = [g["code"] for g in response.data["results"]]
        assert "gw1" in codes

    def test_patch_gateway(self, api_client, gateway):
        response = api_client.patch(
            f"/api/config/scada-gateways/{gateway.id}/",
            {"mqtt_qos": 2}, format="json",
        )

        assert response.status_code == status.HTTP_200_OK
        gateway.refresh_from_db()
        assert gateway.mqtt_qos == 2

    def test_delete_gateway(self, api_client, gateway):
        response = api_client.delete(f"/api/config/scada-gateways/{gateway.id}/")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not config_models.ScadaGateway.objects.filter(id=gateway.id).exists()

    def test_delete_gateway_keeps_devices(self, api_client, site, gateway):
        """SET_NULL: deleting a gateway must not delete the machines behind it."""
        device = _make_device(site, gateway=gateway)

        api_client.delete(f"/api/config/scada-gateways/{gateway.id}/")

        device.refresh_from_db()
        assert device.gateway is None

    def test_device_count_reflects_linked_devices(self, api_client, site, gateway):
        _make_device(site, gateway=gateway, code="scada-gw1-A")
        _make_device(site, gateway=gateway, code="scada-gw1-B")
        # a device on no gateway must not be counted
        config_models.Device.objects.create(
            site=site, code="PLC-9", name="plc", protocol="modbus_tcp",
            ip_address="1.2.3.4", port=502, metadata={},
        )

        response = api_client.get(f"/api/config/scada-gateways/{gateway.id}/")

        assert response.data["device_count"] == 2

    def test_code_must_be_unique(self, api_client, gateway):
        response = api_client.post("/api/config/scada-gateways/", {
            "code": "gw1", "name": "dup",
        }, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------


PROVISION_PAYLOAD = {
    "devices": [
        {
            "device_name": "A0201010001150403",
            "name": "注塑机1",
            "points": [
                {"code": "N270400150027", "description": "注射压力实际值",
                 "data_type": "float", "unit": "MPa"},
                {"code": "N270400150028", "description": "锁模力",
                 "data_type": "float", "unit": ""},
            ],
        },
        {
            "device_name": "A0201010001150404",
            "name": "注塑机2",
            "points": [
                {"code": "N270400150027", "description": "注射压力实际值",
                 "data_type": "float", "unit": "MPa"},
            ],
        },
    ],
    "task": {
        "code": "task-zhongshan",
        "name": "中山小家电注塑采集",
        "sample_rate_hz": 1,
        "is_active": True,
    },
}


def _provision(api_client, gateway, payload=None, site=None):
    body = dict(payload or PROVISION_PAYLOAD)
    if site is not None:
        body["site"] = site.id
    return api_client.post(
        f"/api/config/scada-gateways/{gateway.id}/provision/", body, format="json",
    )


@pytest.mark.django_db
class TestProvision:
    def test_creates_devices_points_and_task(self, api_client, site, gateway):
        response = _provision(api_client, gateway, site=site)

        assert response.status_code == status.HTTP_201_CREATED
        body = response.data
        assert body["gateway"] == gateway.id
        assert body["site"] == site.id
        assert body["created"] == {"devices": 2, "points": 3}

        # response shape (frozen contract)
        assert body["devices"][0]["device_name"] == "A0201010001150403"
        assert body["devices"][0]["code"] == "scada-gw1-A0201010001150403"
        assert [p["code"] for p in body["devices"][0]["points"]] == [
            "N270400150027", "N270400150028",
        ]
        # 一设备一任务:2 台设备 → 2 个任务,任务编码由模板 code 加设备名派生。
        task_codes = sorted(t["code"] for t in body["tasks"])
        assert task_codes == [
            "task-zhongshan-A0201010001150403",
            "task-zhongshan-A0201010001150404",
        ]
        # 多设备时 body["task"](单数,旧兼容字段)为 None
        assert body["task"] is None

        device = config_models.Device.objects.get(code="scada-gw1-A0201010001150403")
        assert device.protocol == "scada"
        assert device.gateway == gateway
        assert device.name == "注塑机1"
        # metadata carries ONLY the per-device name — the whole point of the gateway
        assert device.metadata == {"scada_device_name": "A0201010001150403"}
        # mirrored off the gateway so existing list/搜索 UIs show something
        assert device.ip_address == gateway.source_ip
        assert device.port == gateway.source_port

        # 一设备一任务:这台设备(2 个测点)有自己的任务,只绑本设备测点。
        task = config_models.AcqTask.objects.get(code="task-zhongshan-A0201010001150403")
        assert task.points.count() == 2
        assert task.is_active is True
        assert all(p.device == device for p in task.points.all())

    def test_point_stores_data_type_the_importer_way(self, api_client, site, gateway):
        """extra['data_type'] is what read_plan._resolve_data_type reads."""
        _provision(api_client, gateway, site=site)

        point = config_models.Point.objects.get(
            device__code="scada-gw1-A0201010001150403", code="N270400150027",
        )
        assert point.extra == {"data_type": "float", "unit": "MPa", "protocol": "scada"}
        assert point.address == "N270400150027"
        assert point.description == "注射压力实际值"

    def test_is_idempotent(self, api_client, site, gateway):
        first = _provision(api_client, gateway, site=site)
        assert first.status_code == status.HTTP_201_CREATED

        second = _provision(api_client, gateway, site=site)

        assert second.status_code == status.HTTP_200_OK
        assert second.data["created"] == {"devices": 0, "points": 0}
        # no duplicates anywhere
        assert config_models.Device.objects.filter(gateway=gateway).count() == 2
        assert config_models.Point.objects.filter(device__gateway=gateway).count() == 3
        # 一设备一任务:2 台设备 → 恰好 2 个任务,不重复
        gw_tasks = config_models.AcqTask.objects.filter(code__startswith="task-zhongshan-")
        assert gw_tasks.count() == 2
        assert config_models.AcqTask.objects.filter(code="task-zhongshan").count() == 0
        # 每个任务只绑本设备的测点(2 + 1 = 3)
        assert sorted(t.points.count() for t in gw_tasks) == [1, 2]
        # stable ids across runs
        assert first.data["devices"][0]["id"] == second.data["devices"][0]["id"]
        first_tasks = sorted(t["id"] for t in first.data["tasks"])
        second_tasks = sorted(t["id"] for t in second.data["tasks"])
        assert first_tasks == second_tasks

    def test_reprovision_updates_in_place(self, api_client, site, gateway):
        _provision(api_client, gateway, site=site)

        payload = {
            "devices": [{
                "device_name": "A0201010001150403",
                "name": "注塑机一号(改名)",
                "points": [{"code": "N270400150027", "description": "注射压力(改)",
                            "data_type": "int", "unit": "kPa"}],
            }],
        }
        response = _provision(api_client, gateway, payload=payload, site=site)

        assert response.status_code == status.HTTP_200_OK
        device = config_models.Device.objects.get(code="scada-gw1-A0201010001150403")
        assert device.name == "注塑机一号(改名)"
        point = device.points.get(code="N270400150027")
        assert point.description == "注射压力(改)"
        assert point.extra["data_type"] == "int"
        assert point.extra["unit"] == "kPa"

    def test_reprovision_preserves_device_metadata_overrides(self, api_client, site, gateway):
        """A hand-added device-specific exception survives a re-provision."""
        _provision(api_client, gateway, site=site)
        device = config_models.Device.objects.get(code="scada-gw1-A0201010001150403")
        device.metadata = {**device.metadata, "mqtt_qos": 2}
        device.save()

        _provision(api_client, gateway, site=site)

        device.refresh_from_db()
        assert device.metadata["mqtt_qos"] == 2
        assert device.metadata["scada_device_name"] == "A0201010001150403"

    def test_provisioned_device_collects_via_helper(self, api_client, site, gateway):
        """The whole point: a provisioned device yields a working protocol config."""
        _provision(api_client, gateway, site=site)
        device = config_models.Device.objects.select_related("gateway").get(
            code="scada-gw1-A0201010001150403",
        )

        cfg = build_device_config(device)

        assert cfg["scada_product_key"] == gateway.product_key
        assert cfg["scada_device_name"] == "A0201010001150403"
        assert cfg["mqtt_password"] == "s3cret"

    def test_site_omitted_uses_first_existing_site(self, api_client, site, gateway):
        response = _provision(api_client, gateway)

        assert response.data["site"] == site.id

    def test_site_omitted_and_none_exist_creates_default(self, api_client, gateway):
        config_models.Site.objects.all().delete()

        response = _provision(api_client, gateway)

        created_site = config_models.Site.objects.get(id=response.data["site"])
        assert created_site.code == "default"

    def test_task_is_optional(self, api_client, site, gateway):
        payload = {"devices": [{"device_name": "A1", "name": "机", "points": [
            {"code": "C1", "description": "d", "data_type": "float", "unit": ""},
        ]}]}

        response = _provision(api_client, gateway, payload=payload, site=site)

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["task"] is None
        assert config_models.AcqTask.objects.count() == 0

    def test_device_name_defaults_to_the_device_name_field(self, api_client, site, gateway):
        payload = {"devices": [{"device_name": "A9", "points": []}]}

        response = _provision(api_client, gateway, payload=payload, site=site)

        device = config_models.Device.objects.get(id=response.data["devices"][0]["id"])
        assert device.name == "A9"

    def test_devices_is_required(self, api_client, gateway):
        response = api_client.post(
            f"/api/config/scada-gateways/{gateway.id}/provision/", {}, format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_gateway_404s(self, api_client):
        response = api_client.post(
            "/api/config/scada-gateways/99999/provision/",
            PROVISION_PAYLOAD, format="json",
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
