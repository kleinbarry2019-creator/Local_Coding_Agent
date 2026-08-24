"""Deterministic, fail-closed policy decisions over trusted evidence."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from autonomous_agent.core.config import AgentConfig, ExecutionMode, ResourceLimits

_DOCTOR_READ_CAPABILITY = "doctor.read"
_DOCTOR_LOOPBACK_CAPABILITY = "doctor.ollama.loopback"
_DOCTOR_CAPABILITIES = frozenset(
    {_DOCTOR_READ_CAPABILITY, _DOCTOR_LOOPBACK_CAPABILITY}
)


class SideEffect(str, Enum):
    READ_ONLY = "read-only"
    WRITE_PROJECT = "write-project"
    PROCESS = "process"
    SYSTEM = "system"
    DESTRUCTIVE = "destructive"


class NetworkKind(str, Enum):
    NONE = "none"
    LOOPBACK_DIAGNOSTIC = "loopback-diagnostic"
    OUTBOUND = "outbound"


class DecisionKind(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class ScopeEvidence:
    project_root: Path
    resolved_targets: tuple[Path, ...]
    resolver_id: str
    valid: bool


@dataclass(frozen=True)
class AuthorityGrant:
    grant_id: str
    capabilities: frozenset[str]
    action_digest: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RecoveryEvidence:
    checkpoint_id: str
    checkpoint_digest: str
    protected_scope: tuple[Path, ...]
    verified_at: datetime
    expires_at: datetime
    valid: bool


@dataclass(frozen=True)
class PolicyRequest:
    session_id: str
    request_id: str
    capabilities: frozenset[str]
    side_effect: SideEffect
    requested_targets: tuple[Path, ...]
    network: NetworkKind
    privilege_elevation: bool
    destructive: bool
    requested_timeout_s: float
    requested_output_bytes: int


@dataclass(frozen=True)
class PolicyContext:
    mode: ExecutionMode
    canonical_project_root: Path
    scope: ScopeEvidence | None
    hard_limits: ResourceLimits
    authority: AuthorityGrant | None
    recovery: RecoveryEvidence | None
    doctor_capabilities: frozenset[str]


@dataclass(frozen=True)
class PolicyDecision:
    kind: DecisionKind
    code: str
    explanation: str
    timeout_s: float
    max_output_bytes: int


def build_doctor_context(config: AgentConfig) -> PolicyContext:
    """Build the sole phase-1 trusted context factory."""
    return PolicyContext(
        mode=config.mode,
        canonical_project_root=config.paths.project_root,
        scope=None,
        hard_limits=config.limits,
        authority=None,
        recovery=None,
        doctor_capabilities=_DOCTOR_CAPABILITIES,
    )


def evaluate_policy(
    request: PolicyRequest, context: PolicyContext
) -> PolicyDecision:
    """Evaluate an action claim without executing or mutating anything."""
    if not _metadata_complete(request, context):
        return _decision(
            DecisionKind.DENY,
            "incomplete_request",
            "Policy request or trusted context metadata is incomplete.",
            request,
            context,
        )

    if _budget_exceeded(request, context.hard_limits):
        return _decision(
            DecisionKind.DENY,
            "budget_exceeded",
            "Requested resources exceed the trusted hard limits.",
            request,
            context,
        )

    doctor_read = _is_doctor_read(request, context)
    doctor_loopback = _is_doctor_loopback(request, context)
    if _requires_scope(
        request, context, doctor_read, doctor_loopback
    ) and not _scope_matches(request, context):
        return _decision(
            DecisionKind.DENY,
            "invalid_scope",
            "Trusted scope evidence does not cover every requested target.",
            request,
            context,
        )

    if doctor_read:
        return _decision(
            DecisionKind.ALLOW,
            "doctor_read",
            "The fixed read-only doctor diagnostic is allowed.",
            request,
            context,
        )

    if doctor_loopback:
        return _decision(
            DecisionKind.ALLOW,
            "doctor_loopback",
            "The fixed bounded doctor loopback diagnostic is allowed.",
            request,
            context,
        )

    if context.mode is ExecutionMode.UNRESTRICTED_ROOT:
        return _decision(
            DecisionKind.DENY,
            "authority_unavailable",
            "Unrestricted-root authority is unavailable in phase 1.",
            request,
            context,
        )

    network_confirmation: str | None = None
    if request.network is NetworkKind.OUTBOUND:
        if context.mode is ExecutionMode.AUTONOMOUS:
            return _decision(
                DecisionKind.DENY,
                "network_forbidden",
                "Autonomous outbound network access is not permitted.",
                request,
                context,
            )
        network_confirmation = "Outbound network access requires confirmation."

    if request.network is NetworkKind.LOOPBACK_DIAGNOSTIC:
        if context.mode is ExecutionMode.AUTONOMOUS:
            return _decision(
                DecisionKind.DENY,
                "network_forbidden",
                "Only the fixed doctor loopback diagnostic is permitted.",
                request,
                context,
            )
        network_confirmation = "Unregistered network access requires confirmation."

    if request.privilege_elevation:
        return _decision(
            DecisionKind.DENY,
            "elevation_forbidden",
            "Privilege elevation is forbidden.",
            request,
            context,
        )

    authority_required = request.side_effect in {
        SideEffect.SYSTEM,
        SideEffect.DESTRUCTIVE,
    } or request.destructive
    recovery_required = request.destructive or (
        request.side_effect is SideEffect.DESTRUCTIVE
    )

    now = datetime.now(UTC)
    if authority_required and not _authority_matches(request, context, now):
        return _decision(
            DecisionKind.DENY,
            "authority_unavailable",
            "A fresh action-scoped authority grant is unavailable.",
            request,
            context,
        )

    if recovery_required and not _recovery_matches(request, context, now):
        return _decision(
            DecisionKind.DENY,
            "recovery_required",
            "Matching fresh recovery evidence is required.",
            request,
            context,
        )

    if authority_required:
        return _decision(
            DecisionKind.DENY,
            "authority_unavailable",
            "Phase 1 has no provider that can authorize system actions.",
            request,
            context,
        )

    if network_confirmation is not None:
        return _decision(
            DecisionKind.CONFIRM,
            "confirmation_required",
            network_confirmation,
            request,
            context,
        )

    if context.mode is ExecutionMode.MONITORED:
        if request.side_effect is SideEffect.READ_ONLY:
            return _decision(
                DecisionKind.ALLOW,
                "project_scope",
                "The read is covered by trusted project scope evidence.",
                request,
                context,
            )
        return _decision(
            DecisionKind.CONFIRM,
            "confirmation_required",
            "This monitored action requires user confirmation.",
            request,
            context,
        )

    return _decision(
        DecisionKind.ALLOW,
        "project_scope",
        "The autonomous action is covered by trusted project scope evidence.",
        request,
        context,
    )


def _metadata_complete(request: object, context: object) -> bool:
    if type(request) is not PolicyRequest or type(context) is not PolicyContext:
        return False
    if not _identifier(request.session_id) or not _identifier(request.request_id):
        return False
    if not _capabilities(request.capabilities):
        return False
    if type(request.side_effect) is not SideEffect:
        return False
    if not _path_tuple(request.requested_targets):
        return False
    if type(request.network) is not NetworkKind:
        return False
    if type(request.privilege_elevation) is not bool:
        return False
    if type(request.destructive) is not bool:
        return False
    if not _positive_number(request.requested_timeout_s):
        return False
    if type(request.requested_output_bytes) is not int:
        return False
    if request.requested_output_bytes <= 0:
        return False
    if not _request_classification_complete(request):
        return False
    if type(context.mode) is not ExecutionMode:
        return False
    if not isinstance(context.canonical_project_root, Path):
        return False
    if not _normalized_absolute_path(context.canonical_project_root):
        return False
    if type(context.hard_limits) is not ResourceLimits:
        return False
    if not _limits_complete(context.hard_limits):
        return False
    if not _capabilities(context.doctor_capabilities, doctor_only=True):
        return False
    if context.scope is not None and not _scope_complete(context.scope):
        return False
    if context.authority is not None and not _authority_complete(context.authority):
        return False
    return context.recovery is None or _recovery_complete(context.recovery)


def _identifier(value: object) -> bool:
    return type(value) is str and bool(value) and value.strip() == value


def _capabilities(value: object, *, doctor_only: bool = False) -> bool:
    if type(value) is not frozenset or not value:
        return False
    if not all(_identifier(item) for item in value):
        return False
    return not doctor_only or all(item.startswith("doctor.") for item in value)


def _path_tuple(value: object) -> bool:
    return type(value) is tuple and all(isinstance(item, Path) for item in value)


def _positive_number(value: object) -> bool:
    if type(value) is float:
        return math.isfinite(value) and value > 0.0
    if type(value) is int:
        return value > 0
    return False


def _request_classification_complete(request: PolicyRequest) -> bool:
    if any(item.startswith("doctor.") for item in request.capabilities):
        return _doctor_classification_complete(request)
    if (
        request.side_effect is SideEffect.READ_ONLY
        and not request.requested_targets
    ):
        return False
    if request.side_effect in {
        SideEffect.WRITE_PROJECT,
        SideEffect.SYSTEM,
        SideEffect.DESTRUCTIVE,
    } and not request.requested_targets:
        return False
    if request.side_effect is SideEffect.DESTRUCTIVE and not request.destructive:
        return False
    return not (
        request.destructive
        and request.side_effect
        not in {SideEffect.SYSTEM, SideEffect.DESTRUCTIVE}
    )


def _doctor_classification_complete(request: PolicyRequest) -> bool:
    fixed_read = (
        request.capabilities == frozenset({_DOCTOR_READ_CAPABILITY})
        and request.network is NetworkKind.NONE
    )
    fixed_loopback = (
        request.capabilities == frozenset({_DOCTOR_LOOPBACK_CAPABILITY})
        and request.network is NetworkKind.LOOPBACK_DIAGNOSTIC
    )
    return (
        (fixed_read or fixed_loopback)
        and request.side_effect is SideEffect.READ_ONLY
        and not request.requested_targets
        and not request.privilege_elevation
        and not request.destructive
    )


def _limits_complete(limits: ResourceLimits) -> bool:
    return _positive_number(limits.hard_command_timeout_s) and (
        type(limits.hard_max_output_bytes) is int
        and limits.hard_max_output_bytes > 0
    )


def _scope_complete(scope: object) -> bool:
    return (
        type(scope) is ScopeEvidence
        and isinstance(scope.project_root, Path)
        and _normalized_absolute_path(scope.project_root)
        and _path_tuple(scope.resolved_targets)
        and _identifier(scope.resolver_id)
        and type(scope.valid) is bool
    )


def _aware_datetime(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _authority_complete(authority: object) -> bool:
    return (
        type(authority) is AuthorityGrant
        and _identifier(authority.grant_id)
        and _capabilities(authority.capabilities)
        and _identifier(authority.action_digest)
        and _aware_datetime(authority.issued_at)
        and _aware_datetime(authority.expires_at)
    )


def _recovery_complete(recovery: object) -> bool:
    return (
        type(recovery) is RecoveryEvidence
        and _identifier(recovery.checkpoint_id)
        and _identifier(recovery.checkpoint_digest)
        and _path_tuple(recovery.protected_scope)
        and bool(recovery.protected_scope)
        and _aware_datetime(recovery.verified_at)
        and _aware_datetime(recovery.expires_at)
        and type(recovery.valid) is bool
    )


def _budget_exceeded(request: PolicyRequest, limits: ResourceLimits) -> bool:
    return (
        request.requested_timeout_s > limits.hard_command_timeout_s
        or request.requested_output_bytes > limits.hard_max_output_bytes
    )


def _is_doctor_read(request: PolicyRequest, context: PolicyContext) -> bool:
    return (
        request.capabilities == frozenset({_DOCTOR_READ_CAPABILITY})
        and request.capabilities <= context.doctor_capabilities
        and request.side_effect is SideEffect.READ_ONLY
        and not request.requested_targets
        and request.network is NetworkKind.NONE
        and not request.privilege_elevation
        and not request.destructive
    )


def _is_doctor_loopback(
    request: PolicyRequest, context: PolicyContext
) -> bool:
    return (
        request.capabilities == frozenset({_DOCTOR_LOOPBACK_CAPABILITY})
        and request.capabilities <= context.doctor_capabilities
        and request.side_effect is SideEffect.READ_ONLY
        and not request.requested_targets
        and request.network is NetworkKind.LOOPBACK_DIAGNOSTIC
        and not request.privilege_elevation
        and not request.destructive
    )


def _requires_scope(
    request: PolicyRequest,
    context: PolicyContext,
    doctor_read: bool,
    doctor_loopback: bool,
) -> bool:
    if doctor_read or doctor_loopback:
        return False
    return bool(request.requested_targets) or context.mode is ExecutionMode.AUTONOMOUS


def _scope_matches(request: PolicyRequest, context: PolicyContext) -> bool:
    scope = context.scope
    if scope is None or not scope.valid:
        return False
    if not scope.resolved_targets:
        return False
    if scope.project_root != context.canonical_project_root:
        return False
    if scope.resolved_targets != request.requested_targets:
        return False
    return all(
        _normalized_absolute_path(target)
        and target.is_relative_to(context.canonical_project_root)
        for target in scope.resolved_targets
    )


def _normalized_absolute_path(path: Path) -> bool:
    return path.is_absolute() and path == Path(os.path.normpath(path))


def _authority_matches(
    request: PolicyRequest, context: PolicyContext, now: datetime
) -> bool:
    authority = context.authority
    if authority is None:
        return False
    return (
        authority.action_digest == request.request_id
        and request.capabilities <= authority.capabilities
        and authority.issued_at <= now < authority.expires_at
    )


def _recovery_matches(
    request: PolicyRequest, context: PolicyContext, now: datetime
) -> bool:
    recovery = context.recovery
    if recovery is None:
        return False
    return (
        recovery.valid
        and recovery.checkpoint_digest == request.request_id
        and recovery.protected_scope == request.requested_targets
        and recovery.verified_at <= now < recovery.expires_at
    )


def _decision(
    kind: DecisionKind,
    code: str,
    explanation: str,
    request: object,
    context: object,
) -> PolicyDecision:
    timeout_s = 0.0
    max_output_bytes = 0
    if type(context) is PolicyContext and type(context.hard_limits) is ResourceLimits:
        limits = context.hard_limits
        if _positive_number(limits.hard_command_timeout_s):
            timeout_s = limits.hard_command_timeout_s
        if (
            type(limits.hard_max_output_bytes) is int
            and limits.hard_max_output_bytes > 0
        ):
            max_output_bytes = limits.hard_max_output_bytes
        if type(request) is PolicyRequest:
            if _positive_number(request.requested_timeout_s):
                timeout_s = min(float(request.requested_timeout_s), timeout_s)
            if (
                type(request.requested_output_bytes) is int
                and request.requested_output_bytes > 0
            ):
                max_output_bytes = min(
                    request.requested_output_bytes, max_output_bytes
                )
    return PolicyDecision(
        kind=kind,
        code=code,
        explanation=explanation,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
