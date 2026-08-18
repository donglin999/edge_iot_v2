"""Helpers shared by the versioned contract tests.

The JSON files under ``docs/contracts`` are intentionally independent from
the implementation.  These helpers only normalize live Django/FieldSpec
objects so a test failure presents a useful diff when behavior drifts.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from django.urls import URLResolver, get_resolver
from rest_framework.schemas.generators import EndpointEnumerator


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = REPOSITORY_ROOT / "docs" / "contracts"


def load_contract(filename: str) -> dict[str, Any]:
    return json.loads((CONTRACT_ROOT / filename).read_text(encoding="utf-8"))


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


def current_business_routes() -> list[dict[str, Any]]:
    """Return canonical business routes, excluding DRF format aliases.

    ``DefaultRouter`` emits a second regex for every route (``.json`` etc.).
    The aliases are recorded once as a convention in the fixture; keeping the
    canonical routes here makes the inventory readable and diffable.
    """

    routes: list[dict[str, Any]] = []
    enumerator = EndpointEnumerator()

    def walk(patterns: Iterable, prefix: str = "") -> None:
        for pattern in patterns:
            raw = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, raw)
                continue
            if not raw.startswith(("api/config/", "api/acquisition/")):
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


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


def field_spec(spec) -> dict[str, Any]:
    """Normalize one protocol ``FieldSpec`` into JSON-safe contract data."""

    return {
        "name": spec.name,
        "required": bool(spec.required),
        "kind": spec.kind,
        "default": json_value(spec.default),
        "choices": [json_value(value) for value in (spec.choices or ())],
    }
