"""Base protocol interface and registry for all acquisition protocols.

To add a new protocol see ``protocols/README.md``. The four steps are:

1. Subclass :class:`BaseProtocol` and declare the connection / point fields
   the protocol needs via :data:`DEVICE_FIELDS` and :data:`POINT_FIELDS`.
2. Implement :meth:`connect`, :meth:`disconnect`, :meth:`read_points`,
   :meth:`health_check`.
3. Decorate the class with :meth:`ProtocolRegistry.register`.
4. Add an import in ``protocols/__init__.py`` so registration runs.

The field declarations are the *single source of truth*: the importer uses
them for row-level validation, the API exposes them at
``/api/acquisition/protocols/`` for the frontend to render dynamic forms,
and the Excel template generator uses them to produce per-protocol templates.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Type

logger = logging.getLogger(__name__)


class ProtocolError(Exception):
    """Base exception for protocol-related errors."""


class ConnectionError(ProtocolError):
    """Raised when connection to device fails."""


class ReadError(ProtocolError):
    """Raised when reading data fails."""


# ---------------------------------------------------------------------------
# Field schema
# ---------------------------------------------------------------------------

# Allowed field "kinds" — the frontend renders an input control per kind.
FIELD_KINDS = {"string", "int", "float", "bool", "enum", "secret"}


@dataclass(frozen=True)
class FieldSpec:
    """Declarative description of a configuration field.

    Attributes:
        name: Machine-readable key. Becomes the column name in Excel and the
            dict key in ``device.metadata`` / ``point.extra``.
        label: Human-readable label shown in the UI / Excel template comment.
        kind: One of ``FIELD_KINDS``. Drives the input control + Excel parser.
        required: When True the importer rejects rows where this is missing.
        default: Used when the row omits the field. Ignored if ``required``.
        choices: For ``kind == "enum"``, the allowed values.
        help_text: Tooltip / Excel comment text.
        example: Example value placed in the generated template.
    """

    name: str
    label: str
    kind: str = "string"
    required: bool = False
    default: Any = None
    choices: Optional[Sequence[Any]] = None
    help_text: str = ""
    example: Any = None

    def __post_init__(self) -> None:
        if self.kind not in FIELD_KINDS:
            raise ValueError(f"FieldSpec({self.name}) unknown kind={self.kind}")
        if self.kind == "enum" and not self.choices:
            raise ValueError(f"FieldSpec({self.name}) enum requires choices")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("choices") is not None:
            d["choices"] = list(d["choices"])
        return d


@dataclass(frozen=True)
class ProtocolMeta:
    """High-level info about a protocol (shown in the protocol picker)."""

    name: str
    label: str
    category: str  # "fieldbus" | "industrial-ethernet" | "iot" | "opc"
    description: str = ""
    supports_pause: bool = True


# ---------------------------------------------------------------------------
# BaseProtocol
# ---------------------------------------------------------------------------


class BaseProtocol(ABC):
    """Abstract base class for all protocol implementations."""

    #: Subclasses MUST override with a :class:`ProtocolMeta`.
    META: ProtocolMeta = ProtocolMeta(name="base", label="Base", category="other")

    #: Fields required to *connect* to a device of this protocol. Validated at
    #: import time and rendered in the device add/edit form.
    DEVICE_FIELDS: Sequence[FieldSpec] = ()

    #: Subset of DEVICE_FIELDS used to compute the stable device identity
    #: (``Device.code``) — two rows that share the same identity tuple are
    #: treated as the same device. Must be tuple of field names.
    IDENTITY_FIELDS: Sequence[str] = ()

    #: Fields required to *address a single point* (per-row in Excel).
    POINT_FIELDS: Sequence[FieldSpec] = ()

    def __init__(self, device_config: Dict[str, Any]) -> None:
        self.device_config = device_config
        self.is_connected = False
        self.logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )

    # ------- Lifecycle ------- #
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]: ...

    def read_batch(self, group: "ReadGroup") -> "List[Reading]":  # noqa: F821
        """Execute a single :class:`ReadGroup` and return decoded readings.

        Subclasses opting into the new pipeline (see
        ``acquisition/services/read_plan.py``) must override this. The default
        raises :class:`NotImplementedError` so legacy protocols can still ship
        with only :meth:`read_points`.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement read_batch yet"
        )

    @abstractmethod
    def health_check(self) -> bool: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    # ------- Validation helpers ------- #
    @classmethod
    def validate_device(cls, config: Dict[str, Any]) -> List[str]:
        """Return a list of human-readable error messages, empty on success."""
        return _validate_against_spec(cls.DEVICE_FIELDS, config, scope="device")

    @classmethod
    def validate_point(cls, config: Dict[str, Any]) -> List[str]:
        return _validate_against_spec(cls.POINT_FIELDS, config, scope="point")

    @classmethod
    def coerce_device(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        """Apply defaults + type coercion. Assumes prior `validate_device` pass."""
        return _coerce_against_spec(cls.DEVICE_FIELDS, config)

    @classmethod
    def coerce_point(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        return _coerce_against_spec(cls.POINT_FIELDS, config)


# ---------------------------------------------------------------------------
# Coercion / validation
# ---------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    # pandas NaN — allow without importing pandas
    try:
        import math

        return isinstance(value, float) and math.isnan(value)
    except Exception:  # noqa: BLE001
        return False


def _coerce_enum(value: Any, choices: Optional[Sequence[Any]]) -> Any:
    """Coerce ``value`` into the type of the matching choice.

    Excel readers return ints, floats, and strings interchangeably, so we
    can't rely on the on-the-wire type. We pick the coerced form whose value
    equals one of the choices.
    """
    if choices is None:
        return str(value).strip()
    candidates: List[Any] = [value]
    s = str(value).strip()
    candidates.append(s)
    if s.lstrip("-+").isdigit():
        candidates.append(int(s))
    try:
        candidates.append(float(s))
    except ValueError:
        pass
    for cand in candidates:
        if cand in choices:
            return cand
    return s  # fall through, validator will flag


_COERCERS: Dict[str, Callable[[Any], Any]] = {
    "string": lambda v: str(v).strip(),
    "secret": lambda v: str(v),
    "int": lambda v: int(float(v)),
    "float": lambda v: float(v),
    "bool": lambda v: str(v).strip().lower() in ("1", "true", "yes", "y", "on"),
    # enum requires the choices list — handled specially below
    "enum": lambda v: str(v).strip(),
}


def _coerce_one(spec: FieldSpec, raw: Any) -> Any:
    if spec.kind == "enum":
        return _coerce_enum(raw, spec.choices)
    return _COERCERS[spec.kind](raw)


def _validate_against_spec(
    specs: Sequence[FieldSpec], config: Dict[str, Any], *, scope: str
) -> List[str]:
    errors: List[str] = []
    for spec in specs:
        raw = config.get(spec.name)
        if _is_missing(raw):
            if spec.required and spec.default is None:
                errors.append(f"{scope} 缺少必填字段 `{spec.name}` ({spec.label})")
            continue
        try:
            value = _coerce_one(spec, raw)
        except (TypeError, ValueError) as exc:
            errors.append(
                f"{scope} 字段 `{spec.name}` 无法解析为 {spec.kind}: {raw!r} ({exc})"
            )
            continue
        if spec.kind == "enum" and spec.choices and value not in spec.choices:
            errors.append(
                f"{scope} 字段 `{spec.name}` 取值 {value!r} 不在允许范围 {list(spec.choices)}"
            )
    return errors


def _coerce_against_spec(specs: Sequence[FieldSpec], config: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for spec in specs:
        raw = config.get(spec.name)
        if _is_missing(raw):
            if spec.default is not None:
                out[spec.name] = spec.default
            continue
        try:
            out[spec.name] = _coerce_one(spec, raw)
        except (TypeError, ValueError):
            out[spec.name] = raw  # leave as-is; validate already flagged it
    # Pass-through any extra keys the caller wants preserved (e.g. existing
    # metadata not described by a spec).
    for k, v in config.items():
        out.setdefault(k, v)
    return out


# ---------------------------------------------------------------------------
# ProtocolRegistry
# ---------------------------------------------------------------------------


class ProtocolRegistry:
    """Registry for managing protocol implementations."""

    _protocols: Dict[str, Type[BaseProtocol]] = {}
    _aliases: Dict[str, str] = {}

    @classmethod
    def register(cls, protocol_name: str, *aliases: str) -> Callable[..., Type[BaseProtocol]]:
        """Decorator to register a protocol under a canonical name + aliases."""

        def decorator(protocol_class: Type[BaseProtocol]) -> Type[BaseProtocol]:
            if not issubclass(protocol_class, BaseProtocol):
                raise TypeError(f"{protocol_class} must inherit from BaseProtocol")
            canonical = protocol_name.lower()
            cls._protocols[canonical] = protocol_class
            for alias in aliases:
                cls._aliases[alias.lower()] = canonical
            logger.info("Registered protocol: %s -> %s", canonical, protocol_class.__name__)
            return protocol_class

        return decorator

    @classmethod
    def _resolve(cls, protocol_name: str) -> str:
        key = protocol_name.lower()
        return cls._aliases.get(key, key)

    @classmethod
    def get(cls, protocol_name: str) -> Type[BaseProtocol]:
        """Return the protocol class (raises ValueError if missing)."""
        canonical = cls._resolve(protocol_name)
        if canonical not in cls._protocols:
            raise ValueError(
                f"Protocol '{protocol_name}' not registered. "
                f"Available: {list(cls._protocols.keys())}"
            )
        return cls._protocols[canonical]

    @classmethod
    def create(cls, protocol_name: str, device_config: Dict[str, Any]) -> BaseProtocol:
        """Factory that instantiates the protocol, applying default coercion."""
        klass = cls.get(protocol_name)
        coerced = klass.coerce_device(device_config)
        return klass(coerced)

    @classmethod
    def list_protocols(cls) -> List[str]:
        return sorted(cls._protocols.keys())

    @classmethod
    def describe(cls, protocol_name: str) -> Dict[str, Any]:
        """JSON-serializable description used by `/api/acquisition/protocols/`."""
        klass = cls.get(protocol_name)
        meta = klass.META
        return {
            "name": meta.name,
            "label": meta.label,
            "category": meta.category,
            "description": meta.description,
            "supports_pause": meta.supports_pause,
            "identity_fields": list(klass.IDENTITY_FIELDS),
            "device_fields": [f.to_dict() for f in klass.DEVICE_FIELDS],
            "point_fields": [f.to_dict() for f in klass.POINT_FIELDS],
        }

    @classmethod
    def describe_all(cls) -> List[Dict[str, Any]]:
        return [cls.describe(name) for name in cls.list_protocols()]

    @classmethod
    def clear(cls) -> None:  # pragma: no cover - testing helper
        cls._protocols.clear()
        cls._aliases.clear()
