from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest

from autonomous_agent.core.config import (
    AgentConfig,
    ExecutionMode,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.policy import (
    AuthorityGrant,
    DecisionKind,
    NetworkKind,
    PolicyContext,
    PolicyDecision,
    PolicyRequest,
    RecoveryEvidence,
    ScopeEvidence,
    SideEffect,
    build_doctor_context,
    evaluate_policy,
)

_PAST = datetime(2000, 1, 1, tzinfo=UTC)
_RECENT_PAST = datetime(2025, 1, 1, tzinfo=UTC)
_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)
_DEFAULT_LIMITS = ResourceLimits()


def _config(root: Path, mode: ExecutionMode) -> AgentConfig:
    return AgentConfig(
        schema_version=1,
        mode=mode,
        paths=ResolvedPaths(
            config_file=root / "config.toml",
            project_file=root / ".local-agent.toml",
            project_root=root,
            state_root=root.parent / "state",
        ),
        limits=ResourceLimits(),
        free_only=True,
        audit_required=True,
        provenance=MappingProxyType({}),
    )


def _request(
    root: Path,
    *,
    capabilities: frozenset[str] = frozenset({"project.write"}),
    side_effect: SideEffect = SideEffect.WRITE_PROJECT,
    targets: tuple[Path, ...] | None = None,
    network: NetworkKind = NetworkKind.NONE,
    elevation: bool = False,
    destructive: bool = False,
    timeout_s: float = 5.0,
    output_bytes: int = 4_096,
) -> PolicyRequest:
    return PolicyRequest(
        session_id="session-1",
        request_id="request-1",
        capabilities=capabilities,
        side_effect=side_effect,
        requested_targets=(root / "target",) if targets is None else targets,
        network=network,
        privilege_elevation=elevation,
        destructive=destructive,
        requested_timeout_s=timeout_s,
        requested_output_bytes=output_bytes,
    )


def _scope(
    root: Path,
    targets: tuple[Path, ...],
    *,
    valid: bool = True,
    project_root: Path | None = None,
) -> ScopeEvidence:
    return ScopeEvidence(
        project_root=root if project_root is None else project_root,
        resolved_targets=targets,
        resolver_id="trusted-path-resolver-v1",
        valid=valid,
    )


def _context(
    root: Path,
    mode: ExecutionMode,
    *,
    scope: ScopeEvidence | None = None,
    authority: AuthorityGrant | None = None,
    recovery: RecoveryEvidence | None = None,
    limits: ResourceLimits = _DEFAULT_LIMITS,
) -> PolicyContext:
    return PolicyContext(
        mode=mode,
        canonical_project_root=root,
        scope=scope,
        hard_limits=limits,
        authority=authority,
        recovery=recovery,
        doctor_capabilities=frozenset({"doctor.read", "doctor.ollama.loopback"}),
    )


def _authority(
    request: PolicyRequest,
    *,
    digest: str | None = None,
    issued_at: datetime = _RECENT_PAST,
    expires_at: datetime = _FUTURE,
) -> AuthorityGrant:
    return AuthorityGrant(
        grant_id="grant-1",
        capabilities=request.capabilities,
        action_digest=request.request_id if digest is None else digest,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _recovery(
    request: PolicyRequest,
    *,
    digest: str | None = None,
    protected_scope: tuple[Path, ...] | None = None,
    verified_at: datetime = _RECENT_PAST,
    expires_at: datetime = _FUTURE,
    valid: bool = True,
) -> RecoveryEvidence:
    return RecoveryEvidence(
        checkpoint_id="checkpoint-1",
        checkpoint_digest=request.request_id if digest is None else digest,
        protected_scope=(
            request.requested_targets
            if protected_scope is None
            else protected_scope
        ),
        verified_at=verified_at,
        expires_at=expires_at,
        valid=valid,
    )


@pytest.mark.parametrize(
    ("scenario", "expected_kind", "expected_code"),
    [
        ("doctor_read", DecisionKind.ALLOW, "doctor_read"),
        ("doctor_loopback", DecisionKind.ALLOW, "doctor_loopback"),
        ("monitored_process", DecisionKind.CONFIRM, "confirmation_required"),
        ("monitored_outbound", DecisionKind.CONFIRM, "confirmation_required"),
        ("autonomous_write", DecisionKind.ALLOW, "project_scope"),
        ("autonomous_outside", DecisionKind.DENY, "invalid_scope"),
        ("autonomous_symlink", DecisionKind.DENY, "invalid_scope"),
        ("autonomous_mount", DecisionKind.DENY, "invalid_scope"),
        ("autonomous_elevation", DecisionKind.DENY, "elevation_forbidden"),
        ("unrestricted_claim", DecisionKind.DENY, "authority_unavailable"),
        ("destructive_without_recovery", DecisionKind.DENY, "recovery_required"),
        ("ambiguous", DecisionKind.DENY, "incomplete_request"),
    ],
)
def test_required_policy_matrix(
    tmp_path: Path,
    scenario: str,
    expected_kind: DecisionKind,
    expected_code: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    request = _request(root)
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=_scope(root, request.requested_targets),
    )

    if scenario == "doctor_read":
        request = _request(
            root,
            capabilities=frozenset({"doctor.read"}),
            side_effect=SideEffect.READ_ONLY,
            targets=(),
        )
        context = build_doctor_context(_config(root, ExecutionMode.MONITORED))
    elif scenario == "doctor_loopback":
        request = _request(
            root,
            capabilities=frozenset({"doctor.ollama.loopback"}),
            side_effect=SideEffect.READ_ONLY,
            targets=(),
            network=NetworkKind.LOOPBACK_DIAGNOSTIC,
        )
        context = build_doctor_context(_config(root, ExecutionMode.MONITORED))
    elif scenario == "monitored_process":
        request = _request(
            root,
            capabilities=frozenset({"process.run"}),
            side_effect=SideEffect.PROCESS,
            targets=(),
        )
        context = _context(root, ExecutionMode.MONITORED)
    elif scenario == "monitored_outbound":
        request = _request(
            root,
            capabilities=frozenset({"network.fetch"}),
            side_effect=SideEffect.PROCESS,
            targets=(),
            network=NetworkKind.OUTBOUND,
        )
        context = _context(root, ExecutionMode.MONITORED)
    elif scenario in {
        "autonomous_outside",
        "autonomous_symlink",
        "autonomous_mount",
    }:
        invalid_target = tmp_path / scenario
        request = _request(root, targets=(invalid_target,))
        context = _context(
            root,
            ExecutionMode.AUTONOMOUS,
            scope=_scope(root, (invalid_target,), valid=False),
        )
    elif scenario == "autonomous_elevation":
        request = replace(request, privilege_elevation=True)
    elif scenario == "unrestricted_claim":
        request = replace(
            request,
            capabilities=frozenset({"root.authority"}),
            side_effect=SideEffect.SYSTEM,
        )
        context = _context(
            root,
            ExecutionMode.UNRESTRICTED_ROOT,
            scope=_scope(root, request.requested_targets),
        )
    elif scenario == "destructive_without_recovery":
        request = replace(
            request,
            capabilities=frozenset({"system.destroy"}),
            side_effect=SideEffect.SYSTEM,
            destructive=True,
        )
        context = _context(
            root,
            ExecutionMode.AUTONOMOUS,
            scope=_scope(root, request.requested_targets),
            authority=_authority(request),
        )
    elif scenario == "ambiguous":
        request = replace(request, request_id="")

    decision = evaluate_policy(request, context)

    assert decision.kind is expected_kind
    assert decision.code == expected_code
    assert decision.timeout_s <= context.hard_limits.hard_command_timeout_s
    assert decision.max_output_bytes <= context.hard_limits.hard_max_output_bytes


def test_policy_models_are_frozen_and_string_enums_are_stable(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(FrozenInstanceError):
        request.request_id = "changed"  # type: ignore[misc]

    assert [item.value for item in SideEffect] == [
        "read-only",
        "write-project",
        "process",
        "system",
        "destructive",
    ]
    assert [item.value for item in NetworkKind] == [
        "none",
        "loopback-diagnostic",
        "outbound",
    ]
    assert [item.value for item in DecisionKind] == ["allow", "confirm", "deny"]


def test_doctor_context_has_only_fixed_diagnostic_capabilities(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    context = build_doctor_context(_config(root, ExecutionMode.AUTONOMOUS))

    assert context == PolicyContext(
        mode=ExecutionMode.AUTONOMOUS,
        canonical_project_root=root,
        scope=None,
        hard_limits=ResourceLimits(),
        authority=None,
        recovery=None,
        doctor_capabilities=frozenset(
            {"doctor.read", "doctor.ollama.loopback"}
        ),
    )
    assert all(item.startswith("doctor.") for item in context.doctor_capabilities)


@pytest.mark.parametrize(
    ("claim_name", "claim"),
    [
        ("authority", True),
        ("recovery", {"valid": True}),
    ],
)
def test_request_cannot_carry_trusted_evidence_claims(
    tmp_path: Path, claim_name: str, claim: object
) -> None:
    request = _request(tmp_path)
    values: dict[str, Any] = {
        "session_id": request.session_id,
        "request_id": request.request_id,
        "capabilities": request.capabilities,
        "side_effect": request.side_effect,
        "requested_targets": request.requested_targets,
        "network": request.network,
        "privilege_elevation": request.privilege_elevation,
        "destructive": request.destructive,
        "requested_timeout_s": request.requested_timeout_s,
        "requested_output_bytes": request.requested_output_bytes,
        claim_name: claim,
    }

    with pytest.raises(TypeError):
        PolicyRequest(**values)


@pytest.mark.parametrize(
    ("slot", "forged"),
    [
        ("authority", True),
        ("authority", {"grant_id": "caller-grant"}),
        ("recovery", True),
        ("recovery", {"checkpoint_id": "caller-checkpoint"}),
    ],
)
def test_untyped_context_values_never_become_trusted_evidence(
    tmp_path: Path, slot: str, forged: object
) -> None:
    root = tmp_path / "project"
    request = replace(
        _request(root),
        capabilities=frozenset({"system.destroy"}),
        side_effect=SideEffect.SYSTEM,
        destructive=True,
    )
    context_values: dict[str, object] = {
        "scope": _scope(root, request.requested_targets),
        "authority": _authority(request),
        "recovery": _recovery(request),
    }
    context_values[slot] = forged
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=cast(ScopeEvidence, context_values["scope"]),
        authority=cast(AuthorityGrant, context_values["authority"]),
        recovery=cast(RecoveryEvidence, context_values["recovery"]),
    )

    decision = evaluate_policy(request, context)

    assert decision == PolicyDecision(
        kind=DecisionKind.DENY,
        code="incomplete_request",
        explanation="Policy request or trusted context metadata is incomplete.",
        timeout_s=5.0,
        max_output_bytes=4_096,
    )


@pytest.mark.parametrize(
    "authority",
    [
        AuthorityGrant(
            grant_id="grant-expired",
            capabilities=frozenset({"system.destroy"}),
            action_digest="request-1",
            issued_at=_PAST,
            expires_at=_PAST,
        ),
        AuthorityGrant(
            grant_id="grant-mismatch",
            capabilities=frozenset({"system.destroy"}),
            action_digest="another-request",
            issued_at=_RECENT_PAST,
            expires_at=_FUTURE,
        ),
    ],
)
def test_expired_or_mismatched_authority_denies(
    tmp_path: Path, authority: AuthorityGrant
) -> None:
    root = tmp_path / "project"
    request = replace(
        _request(root),
        capabilities=frozenset({"system.destroy"}),
        side_effect=SideEffect.SYSTEM,
        destructive=True,
    )
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=_scope(root, request.requested_targets),
        authority=authority,
        recovery=_recovery(request),
    )

    decision = evaluate_policy(request, context)

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "authority_unavailable"


@pytest.mark.parametrize(
    "recovery",
    [
        RecoveryEvidence(
            checkpoint_id="checkpoint-expired",
            checkpoint_digest="request-1",
            protected_scope=(Path("/project/target"),),
            verified_at=_PAST,
            expires_at=_PAST,
            valid=True,
        ),
        RecoveryEvidence(
            checkpoint_id="checkpoint-mismatch",
            checkpoint_digest="another-request",
            protected_scope=(Path("/project/target"),),
            verified_at=_RECENT_PAST,
            expires_at=_FUTURE,
            valid=True,
        ),
    ],
)
def test_expired_or_mismatched_recovery_denies(
    tmp_path: Path, recovery: RecoveryEvidence
) -> None:
    root = tmp_path / "project"
    request = replace(
        _request(root),
        capabilities=frozenset({"system.destroy"}),
        side_effect=SideEffect.SYSTEM,
        destructive=True,
    )
    recovery = replace(recovery, protected_scope=request.requested_targets)
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=_scope(root, request.requested_targets),
        authority=_authority(request),
        recovery=recovery,
    )

    decision = evaluate_policy(request, context)

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "recovery_required"


def test_recovery_scope_must_match_all_requested_targets(tmp_path: Path) -> None:
    root = tmp_path / "project"
    request = replace(
        _request(root),
        capabilities=frozenset({"system.destroy"}),
        side_effect=SideEffect.SYSTEM,
        destructive=True,
    )
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=_scope(root, request.requested_targets),
        authority=_authority(request),
        recovery=_recovery(request, protected_scope=(root / "other",)),
    )

    assert evaluate_policy(request, context).code == "recovery_required"


@pytest.mark.parametrize(
    "request_change",
    [
        {"requested_timeout_s": 120.001},
        {"requested_output_bytes": 1_048_577},
    ],
)
def test_requests_above_hard_context_ceiling_deny_before_allowance(
    tmp_path: Path, request_change: dict[str, object]
) -> None:
    root = tmp_path / "project"
    request = _request(
        root,
        capabilities=frozenset({"doctor.read"}),
        side_effect=SideEffect.READ_ONLY,
        targets=(),
    )
    request = replace(request, **request_change)
    context = build_doctor_context(_config(root, ExecutionMode.MONITORED))

    decision = evaluate_policy(request, context)

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "budget_exceeded"
    assert decision.timeout_s <= context.hard_limits.hard_command_timeout_s
    assert decision.max_output_bytes <= context.hard_limits.hard_max_output_bytes


def test_ambiguous_metadata_denies_before_doctor_allowance(tmp_path: Path) -> None:
    root = tmp_path / "project"
    request = _request(
        root,
        capabilities=frozenset({"doctor.read"}),
        side_effect=SideEffect.READ_ONLY,
        targets=(),
    )
    request = replace(request, network=cast(NetworkKind, "none"))

    decision = evaluate_policy(
        request,
        build_doctor_context(_config(root, ExecutionMode.MONITORED)),
    )

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "incomplete_request"


@pytest.mark.parametrize(
    ("elevation", "destructive", "expected_code"),
    [
        (True, False, "elevation_forbidden"),
        (False, True, "recovery_required"),
    ],
)
def test_hard_denials_precede_monitored_network_confirmation(
    tmp_path: Path,
    elevation: bool,
    destructive: bool,
    expected_code: str,
) -> None:
    root = tmp_path / "project"
    request = _request(
        root,
        capabilities=frozenset({"system.destroy"}),
        side_effect=SideEffect.SYSTEM,
        network=NetworkKind.OUTBOUND,
        elevation=elevation,
        destructive=destructive,
    )
    context = _context(
        root,
        ExecutionMode.MONITORED,
        scope=_scope(root, request.requested_targets),
        authority=_authority(request),
    )

    decision = evaluate_policy(request, context)

    assert decision.kind is DecisionKind.DENY
    assert decision.code == expected_code


def test_non_doctor_read_without_a_target_is_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "project"
    request = _request(
        root,
        capabilities=frozenset({"project.read"}),
        side_effect=SideEffect.READ_ONLY,
        targets=(),
    )

    decision = evaluate_policy(
        request,
        _context(root, ExecutionMode.MONITORED),
    )

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "incomplete_request"


def test_evidence_dataclasses_do_not_enable_phase_one_system_actions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    request = _request(
        root,
        capabilities=frozenset({"system.destroy"}),
        side_effect=SideEffect.SYSTEM,
        destructive=True,
    )
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=_scope(root, request.requested_targets),
        authority=_authority(request),
        recovery=_recovery(request),
    )

    decision = evaluate_policy(request, context)

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "authority_unavailable"


def test_empty_scope_cannot_authorize_an_autonomous_process(tmp_path: Path) -> None:
    root = tmp_path / "project"
    request = _request(
        root,
        capabilities=frozenset({"process.run"}),
        side_effect=SideEffect.PROCESS,
        targets=(),
    )
    context = _context(
        root,
        ExecutionMode.AUTONOMOUS,
        scope=_scope(root, ()),
    )

    decision = evaluate_policy(request, context)

    assert decision.kind is DecisionKind.DENY
    assert decision.code == "invalid_scope"
