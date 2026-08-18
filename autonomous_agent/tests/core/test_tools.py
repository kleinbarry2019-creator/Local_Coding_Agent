from __future__ import annotations

import time
from dataclasses import FrozenInstanceError, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, cast

import pytest

from autonomous_agent.core.config import ExecutionMode, ResourceLimits
from autonomous_agent.core.policy import (
    DecisionKind,
    NetworkKind,
    PolicyContext,
    PolicyDecision,
    PolicyRequest,
    SideEffect,
)
from autonomous_agent.core.tools import (
    ArtifactRef,
    ExecutionContext,
    SchemaLimits,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolStatus,
    decode_dataclass,
    encode_dataclass,
)


class Flavor(str, Enum):
    VANILLA = "vanilla"
    CHOCOLATE = "chocolate"


@dataclass(frozen=True)
class NestedInput:
    label: str


@dataclass(frozen=True)
class CompoundInput:
    value: str
    count: int
    ratio: float
    active: bool
    flavor: Flavor
    path: Path
    tags: list[str]
    scores: dict[str, int]
    nested: NestedInput
    optional: str | None


@dataclass(frozen=True)
class ExampleInput:
    value: str


@dataclass(frozen=True)
class ExplodingInput:
    value: str

    def __getattribute__(self, name: str) -> object:
        if name == "value":
            raise RuntimeError("credential=input-secret")
        return super().__getattribute__(name)


@dataclass(frozen=True)
class WideningStringInput:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", self.value * 32)


@dataclass(frozen=True)
class WideningItemsInput:
    values: list[str]

    def __post_init__(self) -> None:
        self.values.extend(["one", "two"])


@dataclass(frozen=True)
class ExampleOutput:
    value: str


@dataclass(frozen=True)
class ExplodingOutput:
    value: str

    def __getattribute__(self, name: str) -> object:
        if name == "value":
            raise RuntimeError("credential=output-secret")
        return super().__getattribute__(name)


@dataclass(frozen=True)
class ListInput:
    values: list[str]


@dataclass(frozen=True)
class DeepInput:
    values: list[list[str]]


@dataclass(frozen=True)
class AnyInput:
    value: Any


@dataclass(frozen=True)
class UnionInput:
    value: str | int


@dataclass(frozen=True)
class NestedAnyInput:
    values: list[Any]


@dataclass(frozen=True)
class InvalidMapInput:
    values: dict[int, str]


@dataclass(frozen=True)
class ArtifactEnvelope:
    artifacts: list[ArtifactRef]


_DEFAULT_SCHEMA_LIMITS = SchemaLimits()


def _policy_context(root: Path, mode: ExecutionMode) -> PolicyContext:
    return PolicyContext(
        mode=mode,
        canonical_project_root=root,
        scope=None,
        hard_limits=ResourceLimits(),
        authority=None,
        recovery=None,
        doctor_capabilities=frozenset(
            {"doctor.read", "doctor.ollama.loopback"}
        ),
    )


def _execution_context(
    root: Path,
    *,
    mode: ExecutionMode = ExecutionMode.MONITORED,
    deadline: float | None = None,
    limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
) -> ExecutionContext:
    return ExecutionContext(
        policy=_policy_context(root, mode),
        deadline_monotonic=(
            time.monotonic() + 30.0 if deadline is None else deadline
        ),
        schema_limits=limits,
    )


def _spec(
    handler: Any,
    *,
    capabilities: frozenset[str] = frozenset({"doctor.read"}),
    side_effect: SideEffect = SideEffect.READ_ONLY,
    network: NetworkKind = NetworkKind.NONE,
    requires_elevation: bool = False,
    requires_recovery: bool = False,
    max_output_bytes: int = 4_096,
) -> ToolSpec[ExampleInput, ExampleOutput]:
    return ToolSpec(
        name="example",
        version="1.2.3",
        description="A bounded example tool.",
        input_type=ExampleInput,
        output_type=ExampleOutput,
        capabilities=capabilities,
        side_effect=side_effect,
        network=network,
        requires_elevation=requires_elevation,
        requires_recovery=requires_recovery,
        default_timeout_s=5.0,
        max_output_bytes=max_output_bytes,
        handler=handler,
    )


def _echo(
    request: ExampleInput, context: ExecutionContext
) -> ExampleOutput:
    del context
    return ExampleOutput(value=request.value)


def test_decode_supports_exact_nested_bounded_types() -> None:
    raw: dict[str, object] = {
        "value": "safe",
        "count": 7,
        "ratio": 1.5,
        "active": True,
        "flavor": "chocolate",
        "path": "/tmp/project",
        "tags": ["one", "two"],
        "scores": {"beta": 2, "alpha": 1},
        "nested": {"label": "inside"},
        "optional": None,
    }

    decoded = decode_dataclass(raw, CompoundInput, SchemaLimits())

    assert decoded == CompoundInput(
        value="safe",
        count=7,
        ratio=1.5,
        active=True,
        flavor=Flavor.CHOCOLATE,
        path=Path("/tmp/project"),
        tags=["one", "two"],
        scores={"alpha": 1, "beta": 2},
        nested=NestedInput(label="inside"),
        optional=None,
    )


def test_encode_returns_only_canonical_json_compatible_objects() -> None:
    value = CompoundInput(
        value="safe",
        count=7,
        ratio=1.5,
        active=True,
        flavor=Flavor.VANILLA,
        path=Path("relative/file"),
        tags=["one"],
        scores={"beta": 2, "alpha": 1},
        nested=NestedInput(label="inside"),
        optional="present",
    )

    encoded = encode_dataclass(value, SchemaLimits())

    assert encoded == {
        "value": "safe",
        "count": 7,
        "ratio": 1.5,
        "active": True,
        "flavor": "vanilla",
        "path": "relative/file",
        "tags": ["one"],
        "scores": {"alpha": 1, "beta": 2},
        "nested": {"label": "inside"},
        "optional": "present",
    }
    scores = encoded["scores"]
    assert isinstance(scores, dict)
    assert list(scores) == ["alpha", "beta"]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"value": "safe", "extra": "authority"},
        {"value": True},
        {"value": 1},
    ],
)
def test_decode_rejects_missing_unknown_and_wrong_primitive_fields(
    raw: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        decode_dataclass(raw, ExampleInput, SchemaLimits())


def test_decode_does_not_confuse_bool_with_int() -> None:
    @dataclass(frozen=True)
    class IntegerInput:
        count: int

    with pytest.raises(ValueError):
        decode_dataclass({"count": True}, IntegerInput, SchemaLimits())


@pytest.mark.parametrize(
    "unsupported_type",
    [AnyInput, UnionInput, NestedAnyInput, InvalidMapInput],
)
def test_codec_rejects_unsupported_or_ambiguous_annotations(
    unsupported_type: type[Any],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_dataclass({}, unsupported_type, SchemaLimits())


@pytest.mark.parametrize(
    ("raw", "limits"),
    [
        ({"value": "0123456789"}, SchemaLimits(max_input_bytes=10)),
        ({"value": "four"}, SchemaLimits(max_string_bytes=3)),
        ({"values": ["a", "b"]}, SchemaLimits(max_items=2)),
    ],
)
def test_decode_rejects_input_byte_string_and_total_item_overflow(
    raw: dict[str, object], limits: SchemaLimits
) -> None:
    expected_type = ListInput if "values" in raw else ExampleInput
    with pytest.raises(ValueError):
        decode_dataclass(raw, expected_type, limits)


def test_decode_rejects_excessive_recursive_depth() -> None:
    with pytest.raises(ValueError):
        decode_dataclass(
            {"values": [["deep"]]},
            DeepInput,
            SchemaLimits(max_depth=2),
        )


def test_encode_validates_runtime_values_and_output_bounds() -> None:
    invalid = cast(ExampleOutput, ExampleOutput(value=cast(str, 7)))

    with pytest.raises(ValueError):
        encode_dataclass(invalid, SchemaLimits())
    with pytest.raises(ValueError):
        encode_dataclass(
            ExampleOutput(value="0123456789"),
            SchemaLimits(max_output_bytes=10),
        )


def test_artifact_metadata_is_subject_to_recursive_schema_caps() -> None:
    artifact = ArtifactRef(
        artifact_id="artifact-too-long",
        media_type="text/plain",
        byte_size=12,
        sha256="deadbeef",
    )

    with pytest.raises(ValueError):
        encode_dataclass(
            ArtifactEnvelope(artifacts=[artifact]),
            SchemaLimits(max_string_bytes=12),
        )


def test_public_models_are_frozen_and_status_values_are_stable(
    tmp_path: Path,
) -> None:
    context = _execution_context(tmp_path)
    result = ToolResult(
        status=ToolStatus.OK,
        data={"value": "safe"},
        diagnostic_code=None,
        diagnostic=None,
        duration_ms=1,
        truncated=False,
        artifacts=(),
    )

    with pytest.raises(FrozenInstanceError):
        context.deadline_monotonic = 0.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.truncated = True  # type: ignore[misc]
    assert [status.value for status in ToolStatus] == [
        "ok",
        "error",
        "denied",
        "confirmation-required",
        "timed-out",
        "invalid-input",
        "invalid-output",
    ]


def test_registry_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register(_spec(_echo))

    with pytest.raises(ValueError):
        registry.register(_spec(_echo))


@pytest.mark.parametrize(
    "spec",
    [
        replace(_spec(_echo), name="Example Tool"),
        replace(_spec(_echo), version="latest"),
        replace(_spec(_echo), capabilities=frozenset()),
        replace(_spec(_echo), default_timeout_s=0.0),
        replace(_spec(_echo), default_timeout_s=float("inf")),
        replace(_spec(_echo), max_output_bytes=0),
        replace(_spec(_echo), max_output_bytes=True),
        replace(
            _spec(_echo),
            input_type=cast(type[ExampleInput], AnyInput),
        ),
    ],
)
def test_registry_rejects_invalid_or_unbounded_specs(
    spec: ToolSpec[ExampleInput, ExampleOutput],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ToolRegistry().register(spec)


@pytest.mark.parametrize(
    "spec",
    [
        replace(_spec(_echo), network=NetworkKind.LOOPBACK_DIAGNOSTIC),
        replace(_spec(_echo), side_effect=SideEffect.PROCESS),
        replace(_spec(_echo), requires_elevation=True),
        replace(_spec(_echo), requires_recovery=True),
        replace(
            _spec(_echo),
            capabilities=frozenset({"doctor.read", "process.run"}),
        ),
        replace(
            _spec(_echo),
            capabilities=frozenset({"project.destroy"}),
            side_effect=SideEffect.DESTRUCTIVE,
            requires_recovery=False,
        ),
        replace(
            _spec(_echo),
            capabilities=frozenset({"process.run"}),
            side_effect=SideEffect.PROCESS,
            requires_recovery=True,
        ),
    ],
)
def test_registry_rejects_contradictory_specs(
    spec: ToolSpec[ExampleInput, ExampleOutput],
) -> None:
    with pytest.raises(ValueError):
        ToolRegistry().register(spec)


def test_confirmation_does_not_call_handler(tmp_path: Path) -> None:
    called = False

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal called
        del context
        called = True
        return ExampleOutput(value=request.value)

    registry = ToolRegistry()
    registry.register(
        _spec(
            handler,
            capabilities=frozenset({"process.run"}),
            side_effect=SideEffect.PROCESS,
        )
    )

    result = registry.execute(
        "example", {"value": "safe"}, _execution_context(tmp_path)
    )

    assert result.status is ToolStatus.CONFIRMATION_REQUIRED
    assert result.diagnostic_code == "confirmation_required"
    assert called is False


def test_denial_does_not_call_handler(tmp_path: Path) -> None:
    called = False

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal called
        del context
        called = True
        return ExampleOutput(value=request.value)

    registry = ToolRegistry()
    registry.register(
        _spec(
            handler,
            capabilities=frozenset({"process.run"}),
            side_effect=SideEffect.PROCESS,
        )
    )

    result = registry.execute(
        "example",
        {"value": "safe"},
        _execution_context(tmp_path, mode=ExecutionMode.AUTONOMOUS),
    )

    assert result.status is ToolStatus.DENIED
    assert called is False


def test_expired_deadline_does_not_call_handler(tmp_path: Path) -> None:
    called = False

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal called
        del context
        called = True
        return ExampleOutput(value=request.value)

    registry = ToolRegistry()
    registry.register(_spec(handler))

    result = registry.execute(
        "example",
        {"value": "safe"},
        _execution_context(tmp_path, deadline=time.monotonic() - 1.0),
    )

    assert result.status is ToolStatus.TIMED_OUT
    assert result.diagnostic_code == "deadline_expired"
    assert called is False


def test_effective_deadline_expiry_immediately_before_handler_does_not_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_called = False
    handler_called = False
    ticks = iter((100.0, 100.0, 100.2, 100.2))

    def monotonic() -> float:
        return next(ticks)

    def policy(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        nonlocal policy_called
        del request, context
        policy_called = True
        return PolicyDecision(
            kind=DecisionKind.ALLOW,
            code="test_allow",
            explanation="Allowed by the test policy boundary.",
            timeout_s=0.1,
            max_output_bytes=1_024,
        )

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal handler_called
        del request, context
        handler_called = True
        return ExampleOutput(value="unsafe")

    monkeypatch.setattr("autonomous_agent.core.tools.time.monotonic", monotonic)
    monkeypatch.setattr("autonomous_agent.core.tools.evaluate_policy", policy)
    registry = ToolRegistry()
    registry.register(replace(_spec(handler), default_timeout_s=0.1))

    result = registry.execute(
        "example",
        {"value": "safe"},
        _execution_context(tmp_path, deadline=200.0),
    )

    assert result.status is ToolStatus.TIMED_OUT
    assert result.diagnostic_code == "deadline_expired"
    assert policy_called is True
    assert handler_called is False


def test_invalid_input_is_rejected_before_policy_and_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal called
        del request, context
        called = True
        return ExampleOutput(value="unsafe")

    def unexpected_policy(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        del request, context
        pytest.fail("policy must not evaluate oversized or invalid input")

    monkeypatch.setattr(
        "autonomous_agent.core.tools.evaluate_policy", unexpected_policy
    )
    registry = ToolRegistry()
    registry.register(_spec(handler))

    result = registry.execute(
        "example",
        {"value": "too long"},
        _execution_context(
            tmp_path,
            limits=SchemaLimits(max_string_bytes=3),
        ),
    )

    assert result.status is ToolStatus.INVALID_INPUT
    assert called is False


def test_input_accessor_exception_is_redacted_before_policy_and_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_called = False
    handler_called = False

    def policy(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        nonlocal policy_called
        del request, context
        policy_called = True
        return PolicyDecision(
            kind=DecisionKind.ALLOW,
            code="test_allow",
            explanation="Allowed by the test policy boundary.",
            timeout_s=2.0,
            max_output_bytes=1_024,
        )

    def handler(
        request: ExplodingInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal handler_called
        del request, context
        handler_called = True
        return ExampleOutput(value="unsafe")

    monkeypatch.setattr("autonomous_agent.core.tools.evaluate_policy", policy)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exploding-input",
            version="1.0.0",
            description="Exercise the input accessor boundary.",
            input_type=ExplodingInput,
            output_type=ExampleOutput,
            capabilities=frozenset({"doctor.read"}),
            side_effect=SideEffect.READ_ONLY,
            network=NetworkKind.NONE,
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=5.0,
            max_output_bytes=4_096,
            handler=handler,
        )
    )

    result = registry.execute(
        "exploding-input",
        {"value": "safe"},
        _execution_context(
            tmp_path,
            limits=SchemaLimits(max_string_bytes=48),
        ),
    )

    assert result.status is ToolStatus.ERROR
    assert result.diagnostic_code == "internal_error"
    assert result.data is None
    assert result.diagnostic is not None
    assert "incident" in result.diagnostic.lower()
    assert "input-secret" not in result.diagnostic
    assert "RuntimeError" not in result.diagnostic
    assert "credential" not in result.diagnostic
    assert len(result.diagnostic.encode("utf-8")) <= 48
    assert policy_called is False
    assert handler_called is False


@pytest.mark.parametrize(
    ("input_type", "raw_input", "limits"),
    [
        pytest.param(
            WideningStringInput,
            {"value": "x"},
            SchemaLimits(max_string_bytes=8),
            id="string-bytes",
        ),
        pytest.param(
            WideningItemsInput,
            {"values": []},
            SchemaLimits(max_items=2),
            id="cumulative-items",
        ),
    ],
)
def test_post_init_cannot_widen_input_after_raw_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_type: type[object],
    raw_input: dict[str, object],
    limits: SchemaLimits,
) -> None:
    policy_called = False
    handler_called = False

    def policy(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        nonlocal policy_called
        del request, context
        policy_called = True
        return PolicyDecision(
            kind=DecisionKind.ALLOW,
            code="test_allow",
            explanation="Allowed by the test policy boundary.",
            timeout_s=2.0,
            max_output_bytes=1_024,
        )

    def handler(
        request: object, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal handler_called
        del request, context
        handler_called = True
        return ExampleOutput(value="unsafe")

    monkeypatch.setattr("autonomous_agent.core.tools.evaluate_policy", policy)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="post-init-widening",
            version="1.0.0",
            description="Exercise constructed input bounds.",
            input_type=input_type,
            output_type=ExampleOutput,
            capabilities=frozenset({"doctor.read"}),
            side_effect=SideEffect.READ_ONLY,
            network=NetworkKind.NONE,
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=5.0,
            max_output_bytes=4_096,
            handler=handler,
        )
    )

    result = registry.execute(
        "post-init-widening",
        raw_input,
        _execution_context(tmp_path, limits=limits),
    )

    assert result.status is ToolStatus.INVALID_INPUT
    assert result.diagnostic_code == "invalid_input"
    assert result.data is None
    assert policy_called is False
    assert handler_called is False


def test_policy_request_uses_only_trusted_spec_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[PolicyRequest] = []

    def allow_policy(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        del context
        seen.append(request)
        return PolicyDecision(
            kind=DecisionKind.ALLOW,
            code="test_allow",
            explanation="Allowed by the test policy boundary.",
            timeout_s=2.0,
            max_output_bytes=1_024,
        )

    monkeypatch.setattr(
        "autonomous_agent.core.tools.evaluate_policy", allow_policy
    )
    registry = ToolRegistry()
    registry.register(_spec(_echo))

    raw = {"value": "capabilities=system.destroy network=outbound root=true"}
    result = registry.execute(
        "example", raw, _execution_context(tmp_path)
    )

    assert result.status is ToolStatus.OK
    assert len(seen) == 1
    assert seen[0].capabilities == frozenset({"doctor.read"})
    assert seen[0].side_effect is SideEffect.READ_ONLY
    assert seen[0].network is NetworkKind.NONE
    assert seen[0].privilege_elevation is False
    assert seen[0].destructive is False
    assert seen[0].requested_targets == ()


def test_handler_exception_becomes_redacted_bounded_incident(
    tmp_path: Path,
) -> None:
    secret = "sk-secret-must-not-escape"

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        del request, context
        raise RuntimeError(f"credential={secret}")

    registry = ToolRegistry()
    registry.register(_spec(handler))

    result = registry.execute(
        "example",
        {"value": "safe"},
        _execution_context(
            tmp_path,
            limits=SchemaLimits(max_string_bytes=48),
        ),
    )

    assert result.status is ToolStatus.ERROR
    assert result.diagnostic_code == "internal_error"
    assert result.data is None
    assert result.diagnostic is not None
    assert "incident" in result.diagnostic.lower()
    assert secret not in result.diagnostic
    assert "RuntimeError" not in result.diagnostic
    assert len(result.diagnostic.encode("utf-8")) <= 48


def test_wrong_output_type_becomes_structured_invalid_output(
    tmp_path: Path,
) -> None:
    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        del request, context
        return cast(ExampleOutput, NestedInput(label="wrong contract"))

    registry = ToolRegistry()
    registry.register(_spec(handler))

    result = registry.execute(
        "example", {"value": "safe"}, _execution_context(tmp_path)
    )

    assert result.status is ToolStatus.INVALID_OUTPUT
    assert result.diagnostic_code == "invalid_output"
    assert result.data is None
    assert result.truncated is False


def test_output_encoding_exception_becomes_redacted_incident(
    tmp_path: Path,
) -> None:
    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExplodingOutput:
        del request, context
        return ExplodingOutput(value="safe")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="exploding-output",
            version="1.0.0",
            description="Exercise the output encoding boundary.",
            input_type=ExampleInput,
            output_type=ExplodingOutput,
            capabilities=frozenset({"doctor.read"}),
            side_effect=SideEffect.READ_ONLY,
            network=NetworkKind.NONE,
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=5.0,
            max_output_bytes=4_096,
            handler=handler,
        )
    )

    result = registry.execute(
        "exploding-output",
        {"value": "safe"},
        _execution_context(tmp_path),
    )

    assert result.status is ToolStatus.ERROR
    assert result.diagnostic_code == "internal_error"
    assert result.diagnostic is not None
    assert "output-secret" not in result.diagnostic


def test_oversized_serialized_output_is_omitted_and_marked_truncated(
    tmp_path: Path,
) -> None:
    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        del request, context
        return ExampleOutput(value="x" * 200)

    registry = ToolRegistry()
    registry.register(_spec(handler, max_output_bytes=64))

    result = registry.execute(
        "example", {"value": "safe"}, _execution_context(tmp_path)
    )

    assert result.status is ToolStatus.INVALID_OUTPUT
    assert result.diagnostic_code == "output_too_large"
    assert result.data is None
    assert result.truncated is True


def test_policy_diagnostics_are_capped_without_handler_execution(
    tmp_path: Path,
) -> None:
    called = False

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        nonlocal called
        del request, context
        called = True
        return ExampleOutput(value="unsafe")

    registry = ToolRegistry()
    registry.register(
        _spec(
            handler,
            capabilities=frozenset({"process.run"}),
            side_effect=SideEffect.PROCESS,
        )
    )

    result = registry.execute(
        "example",
        {"value": "ok"},
        _execution_context(
            tmp_path,
            mode=ExecutionMode.AUTONOMOUS,
            limits=SchemaLimits(max_string_bytes=12),
        ),
    )

    assert result.status is ToolStatus.DENIED
    assert result.diagnostic is not None
    assert len(result.diagnostic.encode("utf-8")) <= 12
    assert called is False


def test_unknown_tool_is_a_structured_expected_failure(tmp_path: Path) -> None:
    result = ToolRegistry().execute(
        "missing", {"value": "safe"}, _execution_context(tmp_path)
    )

    assert result.status is ToolStatus.INVALID_INPUT
    assert result.diagnostic_code == "unknown_tool"
    assert result.data is None


def test_allowed_handler_receives_policy_tightened_deadline_and_output_cap(
    tmp_path: Path,
) -> None:
    original_deadline = time.monotonic() + 30.0
    seen: list[ExecutionContext] = []

    def handler(
        request: ExampleInput, context: ExecutionContext
    ) -> ExampleOutput:
        seen.append(context)
        return ExampleOutput(value=request.value)

    registry = ToolRegistry()
    registry.register(_spec(handler, max_output_bytes=512))

    result = registry.execute(
        "example",
        {"value": "safe"},
        _execution_context(tmp_path, deadline=original_deadline),
    )

    assert result.status is ToolStatus.OK
    assert len(seen) == 1
    assert seen[0].deadline_monotonic <= original_deadline
    assert seen[0].deadline_monotonic <= time.monotonic() + 5.0
    assert seen[0].schema_limits.max_output_bytes == 512
