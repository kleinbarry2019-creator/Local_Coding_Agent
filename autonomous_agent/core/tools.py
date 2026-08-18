"""Bounded typed tool contracts behind the phase-1 policy boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import (
    Any,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from autonomous_agent.core.policy import (
    DecisionKind,
    NetworkKind,
    PolicyContext,
    PolicyDecision,
    PolicyRequest,
    SideEffect,
    evaluate_policy,
)

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_CAPABILITY_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
)
_DOCTOR_READ = frozenset({"doctor.read"})
_DOCTOR_LOOPBACK = frozenset({"doctor.ollama.loopback"})
_JSON_ENCODER = json.JSONEncoder(
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)


class _SchemaError(ValueError):
    """Internal classified validation failure with no untrusted values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SchemaLimits:
    max_input_bytes: int = 65_536
    max_output_bytes: int = 65_536
    max_depth: int = 8
    max_items: int = 1_000
    max_string_bytes: int = 65_536

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{item.name} must be a positive integer")


@dataclass(frozen=True)
class ExecutionContext:
    policy: PolicyContext
    deadline_monotonic: float
    schema_limits: SchemaLimits


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    CONFIRMATION_REQUIRED = "confirmation-required"
    TIMED_OUT = "timed-out"
    INVALID_INPUT = "invalid-input"
    INVALID_OUTPUT = "invalid-output"


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    media_type: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    data: Mapping[str, object] | None
    diagnostic_code: str | None
    diagnostic: str | None
    duration_ms: int
    truncated: bool
    artifacts: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class ToolSpec[InputT, OutputT]:
    name: str
    version: str
    description: str
    input_type: type[InputT]
    output_type: type[OutputT]
    capabilities: frozenset[str]
    side_effect: SideEffect
    network: NetworkKind
    requires_elevation: bool
    requires_recovery: bool
    default_timeout_s: float
    max_output_bytes: int
    handler: Callable[[InputT, ExecutionContext], OutputT]


def decode_dataclass[InputT](
    raw: Mapping[str, object],
    expected_type: type[InputT],
    limits: SchemaLimits,
) -> InputT:
    """Decode an exact JSON-compatible mapping into a declared dataclass."""
    decoded, _payload = _decode_dataclass_with_payload(
        raw, expected_type, limits
    )
    return decoded


def _decode_dataclass_with_payload[InputT](
    raw: Mapping[str, object],
    expected_type: type[InputT],
    limits: SchemaLimits,
) -> tuple[InputT, bytes]:
    _require_limits(limits)
    annotations = _validate_dataclass_type(expected_type)
    if not isinstance(raw, Mapping):
        raise _SchemaError("invalid_value", "input must be a mapping")
    try:
        document = dict(raw)
    except Exception as error:
        raise _SchemaError("invalid_value", "input mapping is unreadable") from error
    _validate_document(document, limits, limits.max_input_bytes, "input")
    decoded = _decode_dataclass_document(document, expected_type, annotations)
    canonical_input = _encode_dataclass_value(
        decoded, expected_type, annotations
    )
    payload = _validate_document(
        canonical_input, limits, limits.max_input_bytes, "input"
    )
    return cast(InputT, decoded), payload


def encode_dataclass[OutputT](
    value: OutputT, limits: SchemaLimits
) -> Mapping[str, object]:
    """Validate and encode a dataclass as a bounded canonical JSON object."""
    _require_limits(limits)
    value_type = type(value)
    annotations = _validate_dataclass_type(value_type)
    encoded = _encode_dataclass_value(value, value_type, annotations)
    _validate_document(encoded, limits, limits.max_output_bytes, "output")
    return encoded


class ToolRegistry:
    """Static typed registry and the sole phase-1 handler invocation boundary."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec[object, object]] = {}

    def register[InputT, OutputT](
        self, spec: ToolSpec[InputT, OutputT]
    ) -> None:
        """Register one immutable, fully bounded tool specification."""
        _validate_spec(spec)
        if spec.name in self._specs:
            raise ValueError("tool name is already registered")
        self._specs[spec.name] = cast(ToolSpec[object, object], spec)

    def execute(
        self,
        name: str,
        raw_input: Mapping[str, object],
        context: ExecutionContext,
    ) -> ToolResult:
        """Validate, authorize, and invoke one trusted registered handler."""
        started = time.monotonic()
        limits = _context_limits(context)
        if type(name) is not str or name not in self._specs:
            return _result(
                started,
                limits,
                ToolStatus.INVALID_INPUT,
                "unknown_tool",
                "The requested tool is not registered.",
            )
        spec = self._specs[name]
        if not _valid_context(context):
            return _result(
                started,
                limits,
                ToolStatus.INVALID_INPUT,
                "invalid_context",
                "The execution context is invalid.",
            )

        try:
            decoded, payload = _decode_dataclass_with_payload(
                raw_input, spec.input_type, limits
            )
            request = _policy_request(spec, payload)
        except _SchemaError:
            return _result(
                started,
                limits,
                ToolStatus.INVALID_INPUT,
                "invalid_input",
                "The tool input does not match its bounded schema.",
            )
        except Exception:  # noqa: BLE001 - redact input programmer errors
            return _incident_result(
                started, limits, ToolStatus.ERROR, "internal_error"
            )

        try:
            decision = evaluate_policy(request, context.policy)
        except Exception:  # noqa: BLE001 - fail closed at the policy boundary
            return _incident_result(
                started, limits, ToolStatus.DENIED, "policy_error"
            )
        if not _valid_decision(decision):
            return _incident_result(
                started, limits, ToolStatus.DENIED, "policy_error"
            )
        if decision.kind is DecisionKind.DENY:
            return _result(
                started,
                limits,
                ToolStatus.DENIED,
                decision.code,
                decision.explanation,
            )
        if decision.kind is DecisionKind.CONFIRM:
            return _result(
                started,
                limits,
                ToolStatus.CONFIRMATION_REQUIRED,
                decision.code,
                decision.explanation,
            )

        now = time.monotonic()
        if now >= context.deadline_monotonic:
            return _result(
                started,
                limits,
                ToolStatus.TIMED_OUT,
                "deadline_expired",
                "The tool deadline expired before invocation.",
            )
        if not _positive_float(decision.timeout_s) or not _positive_int(
            decision.max_output_bytes
        ):
            return _incident_result(
                started, limits, ToolStatus.DENIED, "policy_error"
            )

        output_cap = min(
            spec.max_output_bytes,
            decision.max_output_bytes,
            limits.max_output_bytes,
        )
        effective_deadline = min(
            context.deadline_monotonic,
            now + spec.default_timeout_s,
            now + decision.timeout_s,
        )
        handler_context = replace(
            context,
            deadline_monotonic=effective_deadline,
            schema_limits=replace(limits, max_output_bytes=output_cap),
        )
        if time.monotonic() >= effective_deadline:
            return _result(
                started,
                limits,
                ToolStatus.TIMED_OUT,
                "deadline_expired",
                "The tool deadline expired before invocation.",
            )
        try:
            output = spec.handler(decoded, handler_context)
        except Exception:  # noqa: BLE001 - redact handler programmer errors
            return _incident_result(
                started, limits, ToolStatus.ERROR, "internal_error"
            )

        if time.monotonic() >= effective_deadline:
            return _result(
                started,
                limits,
                ToolStatus.TIMED_OUT,
                "deadline_expired",
                "The tool exceeded its cooperative deadline.",
            )
        if type(output) is not spec.output_type:
            return _result(
                started,
                limits,
                ToolStatus.INVALID_OUTPUT,
                "invalid_output",
                "The handler returned an invalid output contract.",
            )
        try:
            data = encode_dataclass(output, handler_context.schema_limits)
        except _SchemaError as error:
            too_large = error.code == "output_too_large"
            return _result(
                started,
                limits,
                ToolStatus.INVALID_OUTPUT,
                "output_too_large" if too_large else "invalid_output",
                (
                    "The serialized tool output exceeded its byte cap."
                    if too_large
                    else "The handler output does not match its bounded schema."
                ),
                truncated=too_large,
            )
        except (TypeError, ValueError):
            return _result(
                started,
                limits,
                ToolStatus.INVALID_OUTPUT,
                "invalid_output",
                "The handler output does not match its bounded schema.",
            )
        except Exception:  # noqa: BLE001 - redact output programmer errors
            return _incident_result(
                started, limits, ToolStatus.ERROR, "internal_error"
            )
        return _result(
            started,
            limits,
            ToolStatus.OK,
            None,
            None,
            data=data,
        )


def _require_limits(limits: object) -> SchemaLimits:
    if type(limits) is not SchemaLimits:
        raise TypeError("limits must be SchemaLimits")
    return limits


def _context_limits(context: object) -> SchemaLimits:
    if (
        type(context) is ExecutionContext
        and type(context.schema_limits) is SchemaLimits
    ):
        return context.schema_limits
    return SchemaLimits()


def _valid_context(context: object) -> bool:
    return (
        type(context) is ExecutionContext
        and type(context.policy) is PolicyContext
        and type(context.schema_limits) is SchemaLimits
        and _finite_number(context.deadline_monotonic)
    )


def _finite_number(value: object) -> bool:
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _positive_float(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value > 0.0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_spec(spec: object) -> None:
    if type(spec) is not ToolSpec:
        raise TypeError("spec must be ToolSpec")
    if type(spec.name) is not str or _NAME_PATTERN.fullmatch(spec.name) is None:
        raise ValueError("tool name is invalid")
    if (
        type(spec.version) is not str
        or _VERSION_PATTERN.fullmatch(spec.version) is None
    ):
        raise ValueError("tool version must be numeric semantic versioning")
    if (
        type(spec.description) is not str
        or not spec.description
        or spec.description.strip() != spec.description
    ):
        raise ValueError("tool description is invalid")
    if type(spec.capabilities) is not frozenset or not spec.capabilities:
        raise ValueError("tool capabilities must be a non-empty frozenset")
    if not all(
        type(item) is str and _CAPABILITY_PATTERN.fullmatch(item) is not None
        for item in spec.capabilities
    ):
        raise ValueError("tool capabilities are invalid")
    if type(spec.side_effect) is not SideEffect:
        raise ValueError("tool side effect is invalid")
    if type(spec.network) is not NetworkKind:
        raise ValueError("tool network classification is invalid")
    if type(spec.requires_elevation) is not bool:
        raise ValueError("tool elevation classification is invalid")
    if type(spec.requires_recovery) is not bool:
        raise ValueError("tool recovery classification is invalid")
    if not _positive_float(spec.default_timeout_s):
        raise ValueError("tool timeout must be finite and positive")
    if not _positive_int(spec.max_output_bytes):
        raise ValueError("tool output cap must be a positive integer")
    if not callable(spec.handler):
        raise TypeError("tool handler must be callable")
    _validate_dataclass_type(spec.input_type)
    _validate_dataclass_type(spec.output_type)
    _validate_classification(spec)


def _validate_classification(spec: ToolSpec[object, object]) -> None:
    doctor_claims = spec.capabilities & (_DOCTOR_READ | _DOCTOR_LOOPBACK)
    if doctor_claims:
        fixed_read = (
            spec.capabilities == _DOCTOR_READ
            and spec.side_effect is SideEffect.READ_ONLY
            and spec.network is NetworkKind.NONE
        )
        fixed_loopback = (
            spec.capabilities == _DOCTOR_LOOPBACK
            and spec.side_effect is SideEffect.READ_ONLY
            and spec.network is NetworkKind.LOOPBACK_DIAGNOSTIC
        )
        if (
            not (fixed_read or fixed_loopback)
            or spec.requires_elevation
            or spec.requires_recovery
        ):
            raise ValueError("doctor tool classification is contradictory")
    if spec.side_effect is SideEffect.DESTRUCTIVE and not spec.requires_recovery:
        raise ValueError("destructive tools must require recovery")
    if spec.requires_recovery and spec.side_effect not in {
        SideEffect.SYSTEM,
        SideEffect.DESTRUCTIVE,
    }:
        raise ValueError("recovery requires a system-risk classification")


def _validate_dataclass_type(
    expected_type: object,
    stack: frozenset[type[object]] = frozenset(),
) -> Mapping[str, object]:
    if not isinstance(expected_type, type) or not is_dataclass(expected_type):
        raise TypeError("schema root must be a dataclass type")
    typed = cast(type[object], expected_type)
    if typed in stack:
        raise TypeError("recursive dataclass schemas are unsupported")
    try:
        annotations = cast(dict[str, object], get_type_hints(typed))
    except Exception as error:
        raise TypeError("dataclass annotations could not be resolved") from error
    declared_fields = fields(cast(Any, typed))
    if any(not item.init for item in declared_fields):
        raise TypeError("all schema fields must be constructor fields")
    names = {item.name for item in declared_fields}
    if names != set(annotations):
        raise TypeError("every dataclass field must have one concrete annotation")
    next_stack = stack | {typed}
    for annotation in annotations.values():
        _validate_annotation(annotation, next_stack)
    return annotations


def _validate_annotation(
    annotation: object, stack: frozenset[type[object]]
) -> None:
    if annotation is Any:
        raise TypeError("Any is unsupported")
    if annotation in {str, int, float, bool, type(None), Path}:
        return
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {Union, UnionType}:
        non_none = tuple(item for item in arguments if item is not type(None))
        if len(arguments) != 2 or len(non_none) != 1:
            raise TypeError("only optional unions are supported")
        _validate_annotation(non_none[0], stack)
        return
    if origin is list:
        if len(arguments) != 1:
            raise TypeError("lists require one concrete item annotation")
        _validate_annotation(arguments[0], stack)
        return
    if origin is dict:
        if len(arguments) != 2 or arguments[0] is not str:
            raise TypeError("maps require string keys and one concrete value type")
        _validate_annotation(arguments[1], stack)
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if not tuple(annotation):
            raise TypeError("enum schemas must declare at least one member")
        if not all(
            type(item.value) in {str, int, float, bool} for item in annotation
        ):
            raise TypeError("enum values must be JSON primitives")
        return
    if isinstance(annotation, type) and is_dataclass(annotation):
        _validate_dataclass_type(annotation, stack)
        return
    raise TypeError("schema annotation is unsupported")


def _decode_dataclass_document(
    raw: Mapping[str, object],
    expected_type: type[object],
    annotations: Mapping[str, object] | None = None,
) -> object:
    hints = (
        _validate_dataclass_type(expected_type)
        if annotations is None
        else annotations
    )
    expected_names = {
        item.name for item in fields(cast(Any, expected_type))
    }
    if set(raw) != expected_names:
        raise _SchemaError(
            "invalid_value", "input fields must exactly match the schema"
        )
    values = {
        item.name: _decode_value(raw[item.name], hints[item.name])
        for item in fields(cast(Any, expected_type))
    }
    try:
        return expected_type(**values)
    except Exception as error:
        raise _SchemaError(
            "invalid_value", "dataclass construction failed"
        ) from error


def _decode_value(value: object, annotation: object) -> object:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {Union, UnionType}:
        if value is None:
            return None
        concrete = next(item for item in arguments if item is not type(None))
        return _decode_value(value, concrete)
    if annotation is type(None):
        if value is not None:
            raise _SchemaError("invalid_value", "expected null")
        return None
    if annotation in {str, int, float, bool}:
        if type(value) is not annotation:
            raise _SchemaError("invalid_value", "primitive type mismatch")
        return value
    if annotation is Path:
        if type(value) is not str:
            raise _SchemaError("invalid_value", "path must be a string")
        return Path(value)
    if origin is list:
        if type(value) is not list:
            raise _SchemaError("invalid_value", "expected a list")
        return [_decode_value(item, arguments[0]) for item in value]
    if origin is dict:
        if type(value) is not dict or not all(type(key) is str for key in value):
            raise _SchemaError("invalid_value", "expected a string-keyed map")
        return {
            key: _decode_value(value[key], arguments[1])
            for key in sorted(value)
        }
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        for item in annotation:
            if type(value) is type(item.value) and value == item.value:
                return item
        raise _SchemaError("invalid_value", "enum value is not declared")
    if isinstance(annotation, type) and is_dataclass(annotation):
        if type(value) is not dict:
            raise _SchemaError("invalid_value", "nested dataclass must be a map")
        return _decode_dataclass_document(value, annotation)
    raise _SchemaError("invalid_schema", "schema annotation is unsupported")


def _encode_dataclass_value(
    value: object,
    expected_type: type[object],
    annotations: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if type(value) is not expected_type:
        raise _SchemaError("invalid_value", "dataclass type mismatch")
    hints = (
        _validate_dataclass_type(expected_type)
        if annotations is None
        else annotations
    )
    return {
        item.name: _encode_value(getattr(value, item.name), hints[item.name])
        for item in fields(cast(Any, expected_type))
    }


def _encode_value(value: object, annotation: object) -> object:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {Union, UnionType}:
        if value is None:
            return None
        concrete = next(item for item in arguments if item is not type(None))
        return _encode_value(value, concrete)
    if annotation is type(None):
        if value is not None:
            raise _SchemaError("invalid_value", "expected null")
        return None
    if annotation in {str, int, float, bool}:
        if type(value) is not annotation:
            raise _SchemaError("invalid_value", "primitive type mismatch")
        if annotation is float and not math.isfinite(cast(float, value)):
            raise _SchemaError("invalid_value", "float must be finite")
        return value
    if annotation is Path:
        if not isinstance(value, Path):
            raise _SchemaError("invalid_value", "path type mismatch")
        return str(value)
    if origin is list:
        if type(value) is not list:
            raise _SchemaError("invalid_value", "list type mismatch")
        return [_encode_value(item, arguments[0]) for item in value]
    if origin is dict:
        if type(value) is not dict or not all(type(key) is str for key in value):
            raise _SchemaError("invalid_value", "map type mismatch")
        return {
            key: _encode_value(value[key], arguments[1])
            for key in sorted(value)
        }
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not annotation:
            raise _SchemaError("invalid_value", "enum type mismatch")
        return value.value
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _encode_dataclass_value(value, annotation)
    raise _SchemaError("invalid_schema", "schema annotation is unsupported")


@dataclass
class _ItemCounter:
    count: int = 0


def _validate_document(
    value: object,
    limits: SchemaLimits,
    byte_limit: int,
    direction: str,
) -> bytes:
    counter = _ItemCounter()
    _validate_json_value(value, limits, counter, 0)
    return _bounded_serialized_bytes(value, byte_limit, direction)


def _validate_json_value(
    value: object,
    limits: SchemaLimits,
    counter: _ItemCounter,
    depth: int,
) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _SchemaError("invalid_value", "float must be finite")
        return
    if type(value) is str:
        _validate_string(value, limits)
        return
    if type(value) is list:
        next_depth = depth + 1
        _validate_depth(next_depth, limits)
        _add_items(counter, len(value), limits)
        for item in value:
            _validate_json_value(item, limits, counter, next_depth)
        return
    if type(value) is dict:
        next_depth = depth + 1
        _validate_depth(next_depth, limits)
        _add_items(counter, len(value), limits)
        for key, item in value.items():
            if type(key) is not str:
                raise _SchemaError("invalid_value", "map keys must be strings")
            _validate_string(key, limits)
            _validate_json_value(item, limits, counter, next_depth)
        return
    raise _SchemaError("invalid_value", "value is not JSON-compatible")


def _validate_string(value: str, limits: SchemaLimits) -> None:
    if len(value.encode("utf-8")) > limits.max_string_bytes:
        raise _SchemaError("string_too_large", "string exceeds its byte cap")


def _validate_depth(depth: int, limits: SchemaLimits) -> None:
    if depth > limits.max_depth:
        raise _SchemaError("depth_exceeded", "schema depth exceeds its cap")


def _add_items(counter: _ItemCounter, count: int, limits: SchemaLimits) -> None:
    counter.count += count
    if counter.count > limits.max_items:
        raise _SchemaError("items_exceeded", "schema items exceed their cap")


def _bounded_serialized_bytes(
    value: object, maximum: int, direction: str
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in _JSON_ENCODER.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > maximum:
                raise _SchemaError(
                    f"{direction}_too_large",
                    f"serialized {direction} exceeds its byte cap",
                )
            chunks.append(encoded)
    except _SchemaError:
        raise
    except (TypeError, ValueError) as error:
        raise _SchemaError(
            "invalid_value", "value is not canonical JSON"
        ) from error
    return b"".join(chunks)


def _policy_request(
    spec: ToolSpec[object, object],
    payload: bytes,
) -> PolicyRequest:
    digest = hashlib.sha256(
        spec.name.encode("utf-8")
        + b"\x00"
        + spec.version.encode("utf-8")
        + b"\x00"
        + payload
    ).hexdigest()
    return PolicyRequest(
        session_id="phase-1-tools",
        request_id=digest,
        capabilities=spec.capabilities,
        side_effect=spec.side_effect,
        requested_targets=(),
        network=spec.network,
        privilege_elevation=spec.requires_elevation,
        destructive=(
            spec.requires_recovery
            or spec.side_effect is SideEffect.DESTRUCTIVE
        ),
        requested_timeout_s=spec.default_timeout_s,
        requested_output_bytes=spec.max_output_bytes,
    )


def _valid_decision(decision: object) -> bool:
    return (
        type(decision) is PolicyDecision
        and type(decision.kind) is DecisionKind
        and type(decision.code) is str
        and bool(decision.code)
        and decision.code.strip() == decision.code
        and type(decision.explanation) is str
    )


def _result(
    started: float,
    limits: SchemaLimits,
    status: ToolStatus,
    diagnostic_code: str | None,
    diagnostic: str | None,
    *,
    data: Mapping[str, object] | None = None,
    truncated: bool = False,
) -> ToolResult:
    return ToolResult(
        status=status,
        data=data,
        diagnostic_code=diagnostic_code,
        diagnostic=(
            None if diagnostic is None else _cap_text(diagnostic, limits)
        ),
        duration_ms=max(0, int((time.monotonic() - started) * 1_000)),
        truncated=truncated,
        artifacts=(),
    )


def _incident_result(
    started: float,
    limits: SchemaLimits,
    status: ToolStatus,
    diagnostic_code: str,
) -> ToolResult:
    incident_id = uuid.uuid4().hex[:16]
    return _result(
        started,
        limits,
        status,
        diagnostic_code,
        f"Unexpected tool failure; incident {incident_id}.",
    )


def _cap_text(value: str, limits: SchemaLimits) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limits.max_string_bytes:
        return value
    return encoded[: limits.max_string_bytes].decode("utf-8", errors="ignore")
