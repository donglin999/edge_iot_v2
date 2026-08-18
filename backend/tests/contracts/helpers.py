"""Helpers shared by the versioned contract tests.

The JSON files under ``docs/contracts`` are intentionally independent from
the implementation.  These helpers only normalize live Django/FieldSpec
objects so a test failure presents a useful diff when behavior drifts.
"""
from __future__ import annotations

import json
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from django.urls import URLResolver, get_resolver
from rest_framework.schemas.generators import EndpointEnumerator

from acquisition.protocols.base import FieldSpec


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
    The aliases are recorded once as a convention in the fixture; keeping the
    canonical routes here makes the inventory readable and diffable.
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
    """Serialize every ``FieldSpec`` dataclass field without losing ``None``."""

    return {
        field.name: json_value(getattr(spec, field.name))
        for field in fields(FieldSpec)
    }
