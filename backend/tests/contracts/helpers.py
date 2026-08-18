"""Helpers shared by the versioned contract tests.

The JSON files under ``docs/contracts`` are intentionally independent from
the implementation.  These helpers only normalize live Django/FieldSpec
objects so a test failure presents a useful diff when behavior drifts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from django.urls import URLResolver, get_resolver
from rest_framework.schemas.generators import EndpointEnumerator

from acquisition.protocols.base import FieldSpec


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = REPOSITORY_ROOT / "docs" / "contracts"


# A contract fixture must never become a second copy of a customer's
# identifiers.  FieldSpec examples are production UI/template conveniences,
# not wire-level values, so identifier-shaped examples are represented by
# unmistakably synthetic values while every other FieldSpec attribute remains
# exact.  The mapping is deliberately keyed by schema field, not by a known
# customer value: future on-site values are sanitized as well.
_SYNTHETIC_FIELD_EXAMPLES = {
    "code": "synthetic-point-001",
    "description": "synthetic-description",
    "device_a_tag": "synthetic-device-tag-001",
    "device_code": "synthetic-device-code-001",
    "device_name": "synthetic-device-001",
    "endpoint_url": "opc.tcp://192.0.2.20:4840",
    "mqtt_client_id": "synthetic-client-001",
    "mqtt_username": "synthetic-user",
    "opcua_username": "synthetic-user",
    "product_key": "synthetic-product-key",
    "scada_device_name": "synthetic-device-001",
    "scada_product_key": "synthetic-product-key",
    "serial_port": "/dev/synthetic-contract-port",
    "site_code": "synthetic-site-001",
}


def load_contract(filename: str) -> dict[str, Any]:
    return json.loads((CONTRACT_ROOT / filename).read_text(encoding="utf-8"))


def clean_production_protocol_names() -> list[str]:
    """Enumerate the registry produced by a clean production module import.

    Some unrelated tests import the opt-in Mitsubishi adapter and thereby
    mutate ``ProtocolRegistry`` for the rest of the process.  A fresh Python
    interpreter gives us the registration set produced by
    ``acquisition.protocols.__init__`` itself.  It neither consumes the JSON
    fixture nor mutates/restores the caller's registry, so ordering the
    contract tests late in the full suite cannot make the fixture self-prove.
    """

    marker = "__M1_PROTOCOL_REGISTRY__="
    script = (
        "import json\n"
        "from acquisition.protocols import ProtocolRegistry\n"
        f"print({marker!r} + json.dumps(ProtocolRegistry.list_protocols()))\n"
    )
    environment = os.environ.copy()
    environment.pop("EDGE_ENABLE_SIMULATOR", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT / "backend",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(marker):
            return json.loads(line.removeprefix(marker))
    raise AssertionError(
        "clean protocol registry subprocess did not emit its result marker: "
        f"{completed.stdout!r} {completed.stderr!r}"
    )


def _http_methods(callback) -> list[str]:
    actions = getattr(callback, "actions", None)
    if actions:
        return sorted(method.upper() for method in actions if method != "head")

    view_class = getattr(callback, "view_class", None)
    if view_class is None:
        return []
    return [
        method.upper()
        for method in ("get", "post", "put", "patch", "delete")
        if hasattr(view_class, method)
    ]


def _current_routes(*, include) -> list[dict[str, Any]]:
    """Enumerate matching production URL leaves, excluding format aliases."""

    routes: list[dict[str, Any]] = []
    enumerator = EndpointEnumerator()

    def walk(patterns: Iterable, prefix: str = "") -> None:
        for pattern in patterns:
            raw = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, raw)
                continue
            if not include(raw):
                continue
            if "(?P<format>" in raw or "<drf_format_suffix:format>" in raw:
                continue
            path = enumerator.get_path_from_regex(raw).replace("{pk}", "{id}")
            routes.append(
                {
                    "path": path,
                    "methods": _http_methods(pattern.callback),
                    "name": pattern.name,
                }
            )

    walk(get_resolver().url_patterns)
    return sorted(routes, key=lambda item: (item["path"], item["methods"]))


def current_business_routes() -> list[dict[str, Any]]:
    """Return the complete canonical business API inventory.

    ``DefaultRouter`` emits a second regex for every route (``.json`` etc.).
    Format aliases are outside this canonical-path inventory; keeping one
    route per handler makes the exact fixture readable and diffable.
    """

    return _current_routes(
        include=lambda raw: raw.startswith(("api/config/", "api/acquisition/")),
    )


def current_platform_routes() -> list[dict[str, Any]]:
    """Return every non-business API/schema URL from the production resolver.

    Django's ``admin/`` UI is deliberately outside this REST compatibility
    contract. Everything under ``api/`` or ``schema/`` is in scope, so adding
    a new auth/docs/platform leaf makes the exact fixture comparison fail.
    """

    return _current_routes(
        include=lambda raw: (
            raw.startswith(("api/", "schema/"))
            and not raw.startswith(("api/config/", "api/acquisition/"))
        ),
    )


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def field_spec(spec) -> dict[str, Any]:
    """Serialize every FieldSpec field, sanitizing identifier examples only."""

    serialized = {
        field.name: json_value(getattr(spec, field.name))
        for field in fields(FieldSpec)
    }
    if serialized["example"] is not None and spec.name in _SYNTHETIC_FIELD_EXAMPLES:
        serialized["example"] = _SYNTHETIC_FIELD_EXAMPLES[spec.name]
    if spec.name == "source_ip" and serialized["example"] is not None:
        value = str(serialized["example"])
        if not value.endswith(".example.com"):
            serialized["example"] = "192.0.2.10"
    # OPC-UA examples often embed a device/tag path in the generic ``address``
    # field. Preserve the syntax while ensuring it cannot be mistaken for an
    # on-site namespace identifier.
    if spec.name == "address" and str(serialized["example"] or "").startswith("ns="):
        serialized["example"] = "ns=2;s=SyntheticDevice.SyntheticTag"
    return serialized
