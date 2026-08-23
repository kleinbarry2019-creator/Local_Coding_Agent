"""Stateless, policy-gated readiness diagnostics for the phase-1 agent."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import sqlite3
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from autonomous_agent.core.config import AgentConfig, ExecutionMode
from autonomous_agent.core.policy import NetworkKind, SideEffect, build_doctor_context
from autonomous_agent.core.probes import (
    ProbeError,
    ProcessResult,
    TrustedExecutableResolver,
    get_loopback_json,
    run_bounded_process,
)
from autonomous_agent.core.tools import (
    ExecutionContext,
    SchemaLimits,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolStatus,
)

_SCHEMA_VERSION: Final = 1
_OLLAMA_ENDPOINT: Final = "http://127.0.0.1:11434"
_MAX_SUMMARY_BYTES: Final = 240
_MAX_DATA_STRING_BYTES: Final = 512
_MAX_DATA_ITEMS: Final = 64
_MAX_REPORT_DATA_BYTES: Final = 8_192
_MAX_PROBES: Final = 64
_MAX_DURATION_MS: Final = 86_400_000
_NAME_PATTERN = re.compile(r"^doctor\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_CODE_PATTERN = re.compile(
    r"^doctor\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.[a-z][a-z0-9_-]*)+$"
)
_SAFE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()/,-]{0,159}$")
_SAFE_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,127}$")
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(?:api.?key|authorization|credential|passwd|password|secret|token)"
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{8,}|(?:token|secret|password|passwd|api.?key)\s*[:=]|sk-(?:proj-)?[a-z0-9_-]{12,}|gh[opsu]_[a-z0-9]{12,})"
)
_INCIDENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class ProbeStatus(str, Enum):
    PASS = "pass"  # nosec B105
    WARNING = "warning"
    FAIL = "fail"


class DoctorStatus(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class DoctorProbeInput:
    """The intentionally empty input contract for every fixed doctor tool."""


@dataclass(frozen=True)
class DoctorProbeOutput:
    """Bounded typed intermediate result returned by registered doctor tools."""

    status: ProbeStatus
    code: str
    summary: str
    strings: dict[str, str]
    integers: dict[str, int]
    booleans: dict[str, bool]
    floats: dict[str, float]
    string_lists: dict[str, list[str]]
    truncated: bool

    def __post_init__(self) -> None:
        if type(self.status) is not ProbeStatus:
            raise TypeError("probe status is invalid")
        _validate_code(self.code)
        _validate_summary(self.summary)
        object.__setattr__(self, "strings", _copy_typed_map(self.strings, str))
        object.__setattr__(self, "integers", _copy_typed_map(self.integers, int))
        object.__setattr__(self, "booleans", _copy_typed_map(self.booleans, bool))
        object.__setattr__(self, "floats", _copy_typed_map(self.floats, float))
        object.__setattr__(
            self,
            "string_lists",
            _copy_string_list_map(self.string_lists),
        )
        if type(self.truncated) is not bool:
            raise TypeError("probe truncation flag is invalid")


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: ProbeStatus
    required: bool
    code: str
    summary: str
    data: Mapping[str, object]
    duration_ms: int
    truncated: bool

    def __post_init__(self) -> None:
        _validate_name(self.name)
        if type(self.status) is not ProbeStatus:
            raise TypeError("probe status is invalid")
        if type(self.required) is not bool:
            raise TypeError("probe required flag is invalid")
        _validate_code(self.code, prefix=self.name)
        _validate_summary(self.summary)
        frozen = _validated_probe_data(self.name, self.code, self.data)
        object.__setattr__(self, "data", frozen)
        if (
            type(self.duration_ms) is not int
            or not 0 <= self.duration_ms <= _MAX_DURATION_MS
        ):
            raise TypeError("probe duration is invalid")
        if type(self.truncated) is not bool:
            raise TypeError("probe truncation flag is invalid")


@dataclass(frozen=True)
class DoctorReport:
    schema_version: int
    status: DoctorStatus
    generated_at: str
    mode: str
    free_only: bool
    project_root: str
    probes: tuple[ProbeResult, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != _SCHEMA_VERSION
        ):
            raise ValueError("doctor schema version is unsupported")
        if type(self.status) is not DoctorStatus:
            raise TypeError("doctor status is invalid")
        if type(self.generated_at) is not str or not _valid_generated_at(
            self.generated_at
        ):
            raise ValueError("doctor generation timestamp is invalid")
        if type(self.mode) is not str or self.mode not in {
            item.value for item in ExecutionMode
        }:
            raise ValueError("doctor mode is invalid")
        if type(self.free_only) is not bool:
            raise TypeError("doctor free_only flag is invalid")
        _validate_report_path(self.project_root)
        if type(self.probes) is not tuple or len(self.probes) > _MAX_PROBES:
            raise TypeError("doctor probes are invalid")
        if any(type(item) is not ProbeResult for item in self.probes):
            raise TypeError("doctor probe item is invalid")
        copied = tuple(self.probes)
        if tuple(item.name for item in copied) != tuple(
            sorted(item.name for item in copied)
        ):
            raise ValueError("doctor probes are not canonically ordered")
        if len({item.name for item in copied}) != len(copied):
            raise ValueError("doctor probes contain duplicates")
        object.__setattr__(self, "probes", copied)

    def to_dict(self) -> dict[str, object]:
        """Return a fresh, deterministic schema-version-1 JSON document."""
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "free_only": self.free_only,
            "project_root": self.project_root,
            "probes": [
                {
                    "name": item.name,
                    "status": item.status.value,
                    "required": item.required,
                    "code": item.code,
                    "summary": item.summary,
                    "data": _thaw(item.data),
                    "duration_ms": item.duration_ms,
                    "truncated": item.truncated,
                }
                for item in self.probes
            ],
        }


_REQUIRED_PROBES: Final = frozenset(
    {
        "doctor.git",
        "doctor.project",
        "doctor.python",
        "doctor.sqlite",
        "doctor.state-path",
    }
)

DEFAULT_PROBE_NAMES: Final = tuple(
    sorted(
        {
            "doctor.bubblewrap",
            "doctor.gh",
            "doctor.git",
            "doctor.node",
            "doctor.npm",
            "doctor.nvidia",
            "doctor.ollama-health",
            "doctor.ollama-tags",
            "doctor.podman",
            "doctor.project",
            "doctor.python",
            "doctor.quality",
            "doctor.resources",
            "doctor.runtime-paths",
            "doctor.sqlite",
            "doctor.state-path",
            "doctor.system",
            "doctor.systemd-run",
            "doctor.uv",
        }
    )
)

# Each persisted report field is explicitly typed per probe. Unknown keys and
# keys in the wrong typed bucket fail closed at the aggregation boundary.
_DATA_SCHEMA: Final[Mapping[str, Mapping[str, type[object]]]] = MappingProxyType(
    {
        "doctor.system": {"os": str, "release": str, "architecture": str},
        "doctor.resources": {
            "cpu_count": int,
            "ram_total_mib": int,
            "ram_available_mib": int,
            "swap_total_mib": int,
            "swap_free_mib": int,
            "disk_total_mib": int,
            "disk_free_mib": int,
        },
        "doctor.python": {"version": str, "supported": bool},
        "doctor.git": {
            "version": str,
            "inside_worktree": bool,
            "clean": bool,
        },
        "doctor.project": {
            "project_root": str,
            "exists": bool,
            "is_directory": bool,
        },
        "doctor.state-path": {
            "state_root": str,
            "nearest_existing": str,
            "exists": bool,
            "safe": bool,
            "owner_matches": bool,
            "writable_metadata": bool,
        },
        "doctor.gh": {"version": str},
        "doctor.ollama-health": {
            "version": str,
            "status_code": int,
            "body_bytes": int,
        },
        "doctor.ollama-tags": {
            "models": list,
            "model_count": int,
            "status_code": int,
            "body_bytes": int,
        },
        "doctor.nvidia": {
            "driver_version": str,
            "gpu_names": list,
            "gpu_count": int,
            "vram_total_mib": int,
            "vram_free_mib": int,
        },
        "doctor.podman": {"version": str},
        "doctor.bubblewrap": {"version": str},
        "doctor.systemd-run": {"version": str},
        "doctor.node": {"version": str},
        "doctor.npm": {"version": str},
        "doctor.uv": {"version": str},
        "doctor.quality": {
            "available": list,
            "missing": list,
            "bandit_path": str,
            "mypy_path": str,
            "pytest_path": str,
            "ruff_path": str,
        },
        "doctor.runtime-paths": {
            "config_file": str,
            "project_file": str,
            "state_root": str,
            "config_exists": bool,
            "project_config_exists": bool,
            "state_exists": bool,
        },
        "doctor.sqlite": {"version": str, "fts5": bool},
    }
)


class Doctor:
    """Aggregate a fixed set of registered, policy-gated diagnostic probes."""

    def __init__(
        self,
        config: AgentConfig,
        registry: ToolRegistry,
        probe_names: tuple[str, ...],
    ) -> None:
        if type(config) is not AgentConfig:
            raise TypeError("doctor configuration is invalid")
        if type(registry) is not ToolRegistry:
            raise TypeError("doctor registry is invalid")
        if (
            type(probe_names) is not tuple
            or not probe_names
            or len(probe_names) > _MAX_PROBES
        ):
            raise ValueError("doctor probe names are invalid")
        for name in probe_names:
            _validate_name(name)
        if len(set(probe_names)) != len(probe_names):
            raise ValueError("doctor probe names contain duplicates")
        self._config = config
        self._registry = registry
        self._probe_names = tuple(sorted(probe_names))

    def run(self) -> DoctorReport:
        """Run every requested probe without opening persistent agent state."""
        try:
            generated_at = _generated_at()
            if not _valid_generated_at(generated_at):
                raise ValueError
        except Exception:  # noqa: BLE001 - hostile clock boundary
            generated_at = "1970-01-01T00:00:00Z"
        results: list[ProbeResult] = []
        try:
            mode = object.__getattribute__(self._config, "mode")
            paths = object.__getattribute__(self._config, "paths")
            free_only = object.__getattribute__(self._config, "free_only")
            limits = object.__getattribute__(self._config, "limits")
            project_root = object.__getattribute__(paths, "project_root")
            project_root_text = str(project_root)
            timeout = object.__getattribute__(limits, "doctor_probe_timeout_s")
            max_output = object.__getattribute__(limits, "max_output_bytes")
            policy = build_doctor_context(self._config)
            if (
                type(mode) is not ExecutionMode
                or type(free_only) is not bool
                or not isinstance(project_root, Path)
                or type(timeout) is not float
                or not math.isfinite(timeout)
                or timeout <= 0.0
                or type(max_output) is not int
                or max_output <= 0
                or not project_root_text
            ):
                raise TypeError
        except Exception:  # noqa: BLE001 - redacted configuration boundary
            mode = ExecutionMode.MONITORED
            free_only = True
            project_root = Path("/")
            project_root_text = "/"
            timeout = 1.0
            max_output = 4_096
            policy = None

        for name in self._probe_names:
            required = name in _REQUIRED_PROBES
            if policy is None:
                results.append(_incident(name, required, 0))
                continue
            started = time.monotonic()
            try:
                context = ExecutionContext(
                    policy=policy,
                    deadline_monotonic=started + timeout,
                    schema_limits=SchemaLimits(
                        max_input_bytes=1_024,
                        max_output_bytes=min(max_output, 32_768),
                        max_depth=6,
                        max_items=256,
                        max_string_bytes=_MAX_DATA_STRING_BYTES,
                    ),
                )
                tool_result = self._registry.execute(name, {}, context)
                elapsed = _elapsed_ms(started)
                results.append(
                    _normalize_tool_result(name, required, tool_result, elapsed)
                )
            except Exception:  # noqa: BLE001 - isolate each untrusted probe
                results.append(_incident(name, required, _elapsed_ms(started)))

        if any(
            item.required and item.status is not ProbeStatus.PASS for item in results
        ):
            status = DoctorStatus.UNHEALTHY
        elif any(item.status is not ProbeStatus.PASS for item in results):
            status = DoctorStatus.WARNING
        else:
            status = DoctorStatus.HEALTHY
        return DoctorReport(
            schema_version=_SCHEMA_VERSION,
            status=status,
            generated_at=generated_at,
            mode=mode.value,
            free_only=free_only,
            project_root=project_root_text,
            probes=tuple(results),
        )


def build_doctor_registry(config: AgentConfig) -> ToolRegistry:
    """Build the complete static phase-1 doctor registry for ``config``."""
    if type(config) is not AgentConfig:
        raise TypeError("doctor configuration is invalid")
    registry = ToolRegistry()
    handlers: Mapping[str, Callable[[ExecutionContext], DoctorProbeOutput]] = {
        "doctor.bubblewrap": lambda context: _version_probe(
            "doctor.bubblewrap", "bwrap", ("--version",), context
        ),
        "doctor.gh": lambda context: _version_probe(
            "doctor.gh", "gh", ("--version",), context
        ),
        "doctor.git": lambda context: _git_probe(config, context),
        "doctor.node": lambda context: _version_probe(
            "doctor.node", "node", ("--version",), context
        ),
        "doctor.npm": lambda context: _version_probe(
            "doctor.npm", "npm", ("--version",), context
        ),
        "doctor.nvidia": _nvidia_probe,
        "doctor.ollama-health": _ollama_health_probe,
        "doctor.ollama-tags": _ollama_tags_probe,
        "doctor.podman": lambda context: _version_probe(
            "doctor.podman", "podman", ("--version",), context
        ),
        "doctor.project": lambda context: _project_probe(config, context),
        "doctor.python": _python_probe,
        "doctor.quality": _quality_probe,
        "doctor.resources": lambda context: _resources_probe(config, context),
        "doctor.runtime-paths": lambda context: _runtime_paths_probe(config, context),
        "doctor.sqlite": _sqlite_probe,
        "doctor.state-path": lambda context: _state_path_probe(config, context),
        "doctor.system": _system_probe,
        "doctor.systemd-run": lambda context: _version_probe(
            "doctor.systemd-run", "systemd-run", ("--version",), context
        ),
        "doctor.uv": lambda context: _version_probe(
            "doctor.uv", "uv", ("--version",), context
        ),
    }
    for name in DEFAULT_PROBE_NAMES:
        loopback = name.startswith("doctor.ollama-")
        registry.register(
            ToolSpec(
                name=name,
                version="1.0.0",
                description="A fixed bounded local readiness diagnostic.",
                input_type=DoctorProbeInput,
                output_type=DoctorProbeOutput,
                capabilities=frozenset(
                    {"doctor.ollama.loopback" if loopback else "doctor.read"}
                ),
                side_effect=SideEffect.READ_ONLY,
                network=(
                    NetworkKind.LOOPBACK_DIAGNOSTIC if loopback else NetworkKind.NONE
                ),
                requires_elevation=False,
                requires_recovery=False,
                default_timeout_s=config.limits.doctor_probe_timeout_s,
                max_output_bytes=min(config.limits.max_output_bytes, 32_768),
                handler=_guarded_handler(name, handlers[name]),
            )
        )
    return registry


def _guarded_handler(
    name: str, body: Callable[[ExecutionContext], DoctorProbeOutput]
) -> Callable[[DoctorProbeInput, ExecutionContext], DoctorProbeOutput]:
    required = name in _REQUIRED_PROBES

    def guarded(
        request: DoctorProbeInput, context: ExecutionContext
    ) -> DoctorProbeOutput:
        del request
        try:
            result = body(context)
            if type(result) is not DoctorProbeOutput:
                raise TypeError
            return result
        except ProbeError as error:
            if error.code in {"deadline_expired", "probe_timeout"}:
                suffix = "timeout"
                summary = "The diagnostic timed out."
            elif error.code in {
                "executable_not_found",
                "loopback_failed",
                "process_start_failed",
            }:
                suffix = "unavailable"
                summary = "The optional component is unavailable."
            elif error.code == "response_too_large":
                suffix = "truncated"
                summary = "The diagnostic response was truncated."
            else:
                suffix = "failed"
                summary = "The diagnostic failed safely."
            return _probe_output(
                status=ProbeStatus.FAIL if required else ProbeStatus.WARNING,
                code=f"{name}.{suffix}",
                summary=summary,
                truncated=suffix == "truncated",
            )
        except Exception:  # noqa: BLE001 - redact probe implementation errors
            return _probe_output(
                status=ProbeStatus.FAIL if required else ProbeStatus.WARNING,
                code=f"{name}.internal_error",
                summary="The diagnostic failed with an in-memory incident.",
                strings={"incident_id": uuid.uuid4().hex},
            )

    return guarded


def _normalize_tool_result(
    name: str,
    required: bool,
    result: object,
    elapsed_ms: int,
) -> ProbeResult:
    if type(result) is not ToolResult:
        return _incident(name, required, elapsed_ms)
    try:
        status = object.__getattribute__(result, "status")
        data = object.__getattribute__(result, "data")
        diagnostic_code = object.__getattribute__(result, "diagnostic_code")
        duration = object.__getattribute__(result, "duration_ms")
        tool_truncated = object.__getattribute__(result, "truncated")
        if type(status) is not ToolStatus or type(tool_truncated) is not bool:
            raise TypeError
        safe_duration = (
            duration
            if type(duration) is int and 0 <= duration <= _MAX_DURATION_MS
            else elapsed_ms
        )
        if status is ToolStatus.TIMED_OUT:
            return _failure_result(name, required, "timeout", safe_duration)
        if tool_truncated or (
            status is ToolStatus.INVALID_OUTPUT
            and type(diagnostic_code) is str
            and diagnostic_code in {"output_too_large", "tool_truncated"}
        ):
            return _truncated_result(name, required, safe_duration)
        if status is ToolStatus.INVALID_INPUT and diagnostic_code == "unknown_tool":
            return _failure_result(name, required, "unavailable", safe_duration)
        if status is not ToolStatus.OK or not isinstance(data, Mapping):
            return _incident(name, required, safe_duration)
        document = dict(data)
        expected_fields = {
            "status",
            "code",
            "summary",
            "strings",
            "integers",
            "booleans",
            "floats",
            "string_lists",
            "truncated",
        }
        if set(document) != expected_fields:
            raise TypeError
        raw_status = document["status"]
        if type(raw_status) is not str:
            raise TypeError
        probe_status = ProbeStatus(raw_status)
        code = document["code"]
        summary = document["summary"]
        output_truncated = document["truncated"]
        if (
            type(code) is not str
            or type(summary) is not str
            or type(output_truncated) is not bool
        ):
            raise TypeError
        merged = _merge_typed_data(name, document)
        truncated = tool_truncated or output_truncated
        if truncated:
            probe_status = ProbeStatus.FAIL if required else ProbeStatus.WARNING
            code = f"{name}.truncated"
            summary = "The diagnostic response was truncated."
            merged = {}
        elif required and probe_status is not ProbeStatus.PASS:
            probe_status = ProbeStatus.FAIL
        elif not required and probe_status is ProbeStatus.FAIL:
            probe_status = ProbeStatus.WARNING
        return ProbeResult(
            name=name,
            status=probe_status,
            required=required,
            code=code,
            summary=summary,
            data=merged,
            duration_ms=safe_duration,
            truncated=truncated,
        )
    except Exception:  # noqa: BLE001 - hostile mapping/accessor boundary
        return _incident(name, required, elapsed_ms)


def _merge_typed_data(name: str, document: Mapping[str, object]) -> dict[str, object]:
    schema = (
        {"incident_id": str}
        if document.get("code") == f"{name}.internal_error"
        else _DATA_SCHEMA.get(name, {})
    )
    merged: dict[str, object] = {}
    typed_fields: tuple[tuple[str, type[object]], ...] = (
        ("strings", str),
        ("integers", int),
        ("booleans", bool),
        ("floats", float),
        ("string_lists", list),
    )
    for field, expected_type in typed_fields:
        raw = document[field]
        if type(raw) is not dict:
            raise TypeError
        for key, value in cast(dict[object, object], raw).items():
            if (
                type(key) is not str
                or key in merged
                or schema.get(key) is not expected_type
            ):
                raise TypeError
            if expected_type is list:
                if type(value) is not list or any(
                    type(item) is not str for item in value
                ):
                    raise TypeError
                merged[key] = list(cast(list[str], value))
            elif type(value) is not expected_type:
                raise TypeError
            else:
                merged[key] = value
    return merged


def _incident(name: str, required: bool, duration_ms: int) -> ProbeResult:
    return ProbeResult(
        name=name,
        status=ProbeStatus.FAIL if required else ProbeStatus.WARNING,
        required=required,
        code=f"{name}.internal_error",
        summary="The diagnostic failed with an in-memory incident.",
        data={"incident_id": uuid.uuid4().hex},
        duration_ms=min(max(duration_ms, 0), _MAX_DURATION_MS),
        truncated=False,
    )


def _failure_result(
    name: str, required: bool, suffix: str, duration_ms: int
) -> ProbeResult:
    summaries = {
        "timeout": "The diagnostic timed out.",
        "unavailable": "The diagnostic is unavailable.",
    }
    return ProbeResult(
        name=name,
        status=ProbeStatus.FAIL if required else ProbeStatus.WARNING,
        required=required,
        code=f"{name}.{suffix}",
        summary=summaries[suffix],
        data={},
        duration_ms=duration_ms,
        truncated=False,
    )


def _truncated_result(name: str, required: bool, duration_ms: int) -> ProbeResult:
    return ProbeResult(
        name=name,
        status=ProbeStatus.FAIL if required else ProbeStatus.WARNING,
        required=required,
        code=f"{name}.truncated",
        summary="The diagnostic response was truncated.",
        data={},
        duration_ms=duration_ms,
        truncated=True,
    )


def _probe_output(
    *,
    status: ProbeStatus,
    code: str,
    summary: str,
    strings: dict[str, str] | None = None,
    integers: dict[str, int] | None = None,
    booleans: dict[str, bool] | None = None,
    floats: dict[str, float] | None = None,
    string_lists: dict[str, list[str]] | None = None,
    truncated: bool = False,
) -> DoctorProbeOutput:
    return DoctorProbeOutput(
        status=status,
        code=code,
        summary=summary,
        strings={} if strings is None else strings,
        integers={} if integers is None else integers,
        booleans={} if booleans is None else booleans,
        floats={} if floats is None else floats,
        string_lists={} if string_lists is None else string_lists,
        truncated=truncated,
    )


def _system_probe(context: ExecutionContext) -> DoctorProbeOutput:
    del context
    return _probe_output(
        status=ProbeStatus.PASS,
        code="doctor.system.ok",
        summary="Operating system metadata is available.",
        strings={
            "os": _safe_metadata(platform.system()),
            "release": _safe_metadata(platform.release()),
            "architecture": _safe_metadata(platform.machine()),
        },
    )


def _python_probe(context: ExecutionContext) -> DoctorProbeOutput:
    del context
    version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    supported = (3, 12) <= sys.version_info[:2] <= (3, 14)
    return _probe_output(
        status=ProbeStatus.PASS if supported else ProbeStatus.FAIL,
        code="doctor.python.ok" if supported else "doctor.python.unsupported",
        summary=(
            "Python is within the supported range."
            if supported
            else "Python is outside the supported range."
        ),
        strings={"version": version},
        booleans={"supported": supported},
    )


def _resources_probe(
    config: AgentConfig, context: ExecutionContext
) -> DoctorProbeOutput:
    del context
    memory = _memory_info()
    disk = shutil.disk_usage(config.paths.project_root)
    values = {
        "cpu_count": max(os.cpu_count() or 1, 1),
        "ram_total_mib": memory.get("MemTotal", 0) // 1_024,
        "ram_available_mib": memory.get("MemAvailable", 0) // 1_024,
        "swap_total_mib": memory.get("SwapTotal", 0) // 1_024,
        "swap_free_mib": memory.get("SwapFree", 0) // 1_024,
        "disk_total_mib": disk.total // (1_024 * 1_024),
        "disk_free_mib": disk.free // (1_024 * 1_024),
    }
    warning = (
        values["ram_available_mib"] < config.limits.min_free_ram_mib
        or values["disk_free_mib"] < config.limits.min_free_disk_mib
    )
    return _probe_output(
        status=ProbeStatus.WARNING if warning else ProbeStatus.PASS,
        code="doctor.resources.low" if warning else "doctor.resources.ok",
        summary=(
            "Available resources are below a configured warning threshold."
            if warning
            else "Local resource metadata is ready."
        ),
        integers=values,
    )


def _memory_info() -> dict[str, int]:
    with Path("/proc/meminfo").open("rb") as stream:
        payload = stream.read(65_537)
    if len(payload) > 65_536:
        raise ProbeError("metadata_too_large", "resource metadata is too large")
    result: dict[str, int] = {}
    for raw_line in payload.splitlines():
        parts = raw_line.split()
        if len(parts) >= 2 and parts[0].rstrip(b":") in {
            b"MemTotal",
            b"MemAvailable",
            b"SwapTotal",
            b"SwapFree",
        }:
            key = parts[0].rstrip(b":").decode("ascii")
            result[key] = int(parts[1])
    return result


def _project_probe(config: AgentConfig, context: ExecutionContext) -> DoctorProbeOutput:
    del context
    root = config.paths.project_root
    metadata = root.lstat()
    exists = root.exists()
    is_directory = stat.S_ISDIR(metadata.st_mode)
    ready = exists and is_directory and not stat.S_ISLNK(metadata.st_mode)
    return _probe_output(
        status=ProbeStatus.PASS if ready else ProbeStatus.FAIL,
        code="doctor.project.ok" if ready else "doctor.project.unsafe",
        summary="Project root metadata is safe."
        if ready
        else "Project root is unsafe.",
        strings={"project_root": str(root)},
        booleans={"exists": exists, "is_directory": is_directory},
    )


def _state_path_probe(
    config: AgentConfig, context: ExecutionContext
) -> DoctorProbeOutput:
    del context
    root = config.paths.state_root
    safe, nearest, exists, owner_matches, writable = _state_path_metadata(root)
    return _probe_output(
        status=ProbeStatus.PASS if safe and writable else ProbeStatus.FAIL,
        code="doctor.state-path.ok"
        if safe and writable
        else "doctor.state-path.unsafe",
        summary=(
            "State path metadata is safe."
            if safe and writable
            else "State path metadata is unsafe."
        ),
        strings={"state_root": str(root), "nearest_existing": str(nearest)},
        booleans={
            "exists": exists,
            "safe": safe,
            "owner_matches": owner_matches,
            "writable_metadata": writable,
        },
    )


def _state_path_metadata(path: Path) -> tuple[bool, Path, bool, bool, bool]:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        return False, Path(path.anchor or "/"), False, False, False
    current = Path(path.anchor)
    nearest = current
    exists = True
    safe = True
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            exists = False
            break
        except OSError:
            return False, nearest, False, False, False
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            safe = False
            break
        nearest = current
    try:
        target_metadata = nearest.lstat()
        owner_matches = target_metadata.st_uid in {0, os.getuid()}
        writable = os.access(nearest, os.W_OK | os.X_OK)
        if exists:
            owner_matches = target_metadata.st_uid == os.getuid()
    except OSError:
        return False, nearest, exists, False, False
    return safe, nearest, exists, owner_matches, writable and owner_matches


def _runtime_paths_probe(
    config: AgentConfig, context: ExecutionContext
) -> DoctorProbeOutput:
    del context
    paths = config.paths
    return _probe_output(
        status=ProbeStatus.PASS,
        code="doctor.runtime-paths.ok",
        summary="Runtime path metadata is available.",
        strings={
            "config_file": str(paths.config_file),
            "project_file": str(paths.project_file),
            "state_root": str(paths.state_root),
        },
        booleans={
            "config_exists": paths.config_file.exists(),
            "project_config_exists": paths.project_file.exists(),
            "state_exists": paths.state_root.exists(),
        },
    )


def _sqlite_probe(context: ExecutionContext) -> DoctorProbeOutput:
    del context
    fts5 = False
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(content)")
        fts5 = True
    except sqlite3.Error:
        fts5 = False
    finally:
        connection.close()
    return _probe_output(
        status=ProbeStatus.PASS if fts5 else ProbeStatus.FAIL,
        code="doctor.sqlite.ok" if fts5 else "doctor.sqlite.fts5_unavailable",
        summary="SQLite and FTS5 are ready." if fts5 else "SQLite FTS5 is unavailable.",
        strings={"version": sqlite3.sqlite_version},
        booleans={"fts5": fts5},
    )


def _git_probe(config: AgentConfig, context: ExecutionContext) -> DoctorProbeOutput:
    version_result = _command("git", ("--version",), context)
    version = _safe_version(version_result.stdout)
    root = str(config.paths.project_root)
    inside = _command(
        "git", ("-C", root, "rev-parse", "--is-inside-work-tree"), context
    )
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        raise ProbeError("git_worktree_unavailable", "Git worktree is unavailable")
    status_result = _command(
        "git", ("-C", root, "status", "--porcelain=v1", "--untracked-files=no"), context
    )
    if status_result.returncode != 0:
        raise ProbeError("git_status_failed", "Git status failed")
    return _probe_output(
        status=ProbeStatus.PASS,
        code="doctor.git.ok",
        summary="Git and hardened worktree inspection are ready.",
        strings={"version": version},
        booleans={"inside_worktree": True, "clean": not bool(status_result.stdout)},
    )


def _version_probe(
    probe_name: str,
    executable_name: str,
    arguments: tuple[str, ...],
    context: ExecutionContext,
) -> DoctorProbeOutput:
    result = _command(executable_name, arguments, context)
    if result.returncode != 0:
        raise ProbeError("command_failed", "version command failed")
    return _probe_output(
        status=ProbeStatus.PASS,
        code=f"{probe_name}.ok",
        summary="The optional component is available.",
        strings={"version": _safe_version(result.stdout)},
    )


def _quality_probe(context: ExecutionContext) -> DoctorProbeOutput:
    del context
    names = ("bandit", "mypy", "pytest", "ruff")
    resolver = TrustedExecutableResolver()
    available: list[str] = []
    missing: list[str] = []
    paths: dict[str, str] = {}
    for name in names:
        try:
            executable = resolver.resolve(name)
        except ProbeError:
            missing.append(name)
        else:
            available.append(name)
            paths[f"{name}_path"] = str(executable)
    status = ProbeStatus.PASS if not missing else ProbeStatus.WARNING
    return _probe_output(
        status=status,
        code="doctor.quality.ok" if not missing else "doctor.quality.partial",
        summary="Quality tools are ready."
        if not missing
        else "Some quality tools are unavailable.",
        strings=paths,
        string_lists={"available": available, "missing": missing},
    )


def _nvidia_probe(context: ExecutionContext) -> DoctorProbeOutput:
    result = _command(
        "nvidia-smi",
        (
            "--query-gpu=name,memory.total,memory.free,driver_version",
            "--format=csv,noheader,nounits",
        ),
        context,
    )
    if result.returncode != 0:
        raise ProbeError("command_failed", "NVIDIA diagnostic failed")
    rows = [
        line for line in result.stdout.decode("utf-8", "strict").splitlines() if line
    ]
    if not rows or len(rows) > 16:
        raise ProbeError("invalid_output", "NVIDIA output is invalid")
    names: list[str] = []
    total = 0
    free = 0
    driver = ""
    for row in rows:
        parts = [item.strip() for item in row.split(",")]
        if len(parts) != 4:
            raise ProbeError("invalid_output", "NVIDIA output is invalid")
        name, raw_total, raw_free, raw_driver = parts
        names.append(_safe_metadata(name))
        total += int(raw_total)
        free += int(raw_free)
        safe_driver = _safe_version(raw_driver.encode())
        if driver and driver != safe_driver:
            raise ProbeError("invalid_output", "NVIDIA output is inconsistent")
        driver = safe_driver
    return _probe_output(
        status=ProbeStatus.PASS,
        code="doctor.nvidia.ok",
        summary="NVIDIA GPU metadata is available.",
        strings={"driver_version": driver},
        integers={
            "gpu_count": len(rows),
            "vram_total_mib": total,
            "vram_free_mib": free,
        },
        string_lists={"gpu_names": sorted(names)},
    )


def _ollama_health_probe(context: ExecutionContext) -> DoctorProbeOutput:
    response = get_loopback_json(
        _OLLAMA_ENDPOINT,
        "/api/version",
        context.deadline_monotonic,
        context.schema_limits.max_output_bytes,
    )
    if response.status_code != 200 or type(response.data) is not dict:
        raise ProbeError("unhealthy_response", "Ollama health response is unhealthy")
    data = cast(dict[object, object], response.data)
    raw_version = data.get("version")
    version = _safe_metadata(raw_version) if type(raw_version) is str else "available"
    return _probe_output(
        status=ProbeStatus.PASS,
        code="doctor.ollama-health.ok",
        summary="Ollama loopback health is ready.",
        strings={"version": version},
        integers={
            "status_code": response.status_code,
            "body_bytes": response.body_bytes,
        },
    )


def _ollama_tags_probe(context: ExecutionContext) -> DoctorProbeOutput:
    response = get_loopback_json(
        _OLLAMA_ENDPOINT,
        "/api/tags",
        context.deadline_monotonic,
        context.schema_limits.max_output_bytes,
    )
    if response.status_code != 200 or type(response.data) is not dict:
        raise ProbeError("unhealthy_response", "Ollama tags response is unhealthy")
    raw_models = cast(dict[object, object], response.data).get("models")
    if type(raw_models) is not list or len(raw_models) > 32:
        raise ProbeError("invalid_output", "Ollama model inventory is invalid")
    models: list[str] = []
    for entry in cast(list[object], raw_models):
        if type(entry) is not dict:
            raise ProbeError("invalid_output", "Ollama model inventory is invalid")
        raw_name = cast(dict[object, object], entry).get("name")
        if type(raw_name) is not str or _SAFE_MODEL_PATTERN.fullmatch(raw_name) is None:
            raise ProbeError("invalid_output", "Ollama model name is invalid")
        models.append(raw_name)
    models = sorted(set(models))
    return _probe_output(
        status=ProbeStatus.PASS,
        code="doctor.ollama-tags.ok",
        summary="Ollama local model inventory is available.",
        integers={
            "model_count": len(models),
            "status_code": response.status_code,
            "body_bytes": response.body_bytes,
        },
        string_lists={"models": models},
    )


def _command(
    executable_name: str,
    arguments: tuple[str, ...],
    context: ExecutionContext,
) -> ProcessResult:
    executable = TrustedExecutableResolver().resolve(executable_name)
    result = run_bounded_process(
        executable,
        arguments,
        {},
        context.deadline_monotonic,
        min(context.schema_limits.max_output_bytes, 8_192),
    )
    if result.timed_out:
        raise ProbeError("probe_timeout", "probe process timed out")
    if result.truncated:
        raise ProbeError("response_too_large", "probe process output was truncated")
    return result


def _safe_version(payload: bytes) -> str:
    try:
        first_line = payload.decode("utf-8", "strict").splitlines()[0].strip()
    except (IndexError, UnicodeDecodeError):
        return "available"
    if (
        _SAFE_VERSION_PATTERN.fullmatch(first_line) is None
        or _SENSITIVE_TEXT_PATTERN.search(first_line) is not None
    ):
        return "available"
    return first_line


def _safe_metadata(value: object) -> str:
    if type(value) is not str:
        raise ProbeError("invalid_output", "probe metadata is invalid")
    encoded = value.encode("utf-8")
    if (
        not value
        or len(encoded) > 160
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or _SENSITIVE_TEXT_PATTERN.search(value) is not None
    ):
        raise ProbeError("invalid_output", "probe metadata is invalid")
    return value


def _copy_typed_map[T](raw: dict[str, T], expected: type[T]) -> dict[str, T]:
    if type(raw) is not dict or len(raw) > _MAX_DATA_ITEMS:
        raise TypeError("probe typed data is invalid")
    copied: dict[str, T] = {}
    for key, value in raw.items():
        if type(key) is not str or not key or type(value) is not expected:
            raise TypeError("probe typed data is invalid")
        if type(value) is str:
            _validate_data_string(cast(str, value))
        elif (
            type(value) is int
            and not 0 <= cast(int, value) <= 9_223_372_036_854_775_807
        ):
            raise ValueError("probe integer data is invalid")
        elif type(value) is float and (
            not math.isfinite(cast(float, value)) or cast(float, value) < 0.0
        ):
            raise ValueError("probe float data is invalid")
        copied[key] = value
    return copied


def _copy_string_list_map(raw: dict[str, list[str]]) -> dict[str, list[str]]:
    if type(raw) is not dict or len(raw) > _MAX_DATA_ITEMS:
        raise TypeError("probe list data is invalid")
    copied: dict[str, list[str]] = {}
    for key, values in raw.items():
        if (
            type(key) is not str
            or type(values) is not list
            or len(values) > _MAX_DATA_ITEMS
        ):
            raise TypeError("probe list data is invalid")
        items: list[str] = []
        for value in values:
            if type(value) is not str:
                raise TypeError("probe list data is invalid")
            _validate_data_string(value)
            items.append(value)
        copied[key] = items
    return copied


def _validated_probe_data(
    name: str, code: str, raw: Mapping[str, object]
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise TypeError("probe data is invalid")
    try:
        document = dict(raw)
    except Exception:  # noqa: BLE001 - hostile mapping boundary
        raise TypeError("probe data is unreadable") from None
    if code == f"{name}.internal_error":
        schema: Mapping[str, type[object]] = {"incident_id": str}
    else:
        schema = _DATA_SCHEMA.get(name, {})
    if set(document) - set(schema) or len(document) > _MAX_DATA_ITEMS:
        raise ValueError("probe data contains unsupported fields")
    frozen: dict[str, object] = {}
    for key in sorted(document):
        value = document[key]
        expected = schema[key]
        if expected is list:
            if type(value) not in {list, tuple}:
                raise TypeError("probe data field type is invalid")
            values = tuple(cast(list[object] | tuple[object, ...], value))
            if len(values) > _MAX_DATA_ITEMS or any(
                type(item) is not str for item in values
            ):
                raise TypeError("probe data list is invalid")
            for item in values:
                _validate_data_string(cast(str, item))
            frozen[key] = values
        elif type(value) is not expected:
            raise TypeError("probe data field type is invalid")
        else:
            if type(value) is str:
                _validate_data_string(value)
                if (
                    key == "incident_id"
                    and _INCIDENT_ID_PATTERN.fullmatch(value) is None
                ):
                    raise ValueError("probe incident identifier is invalid")
            elif type(value) is int and not 0 <= value <= 9_223_372_036_854_775_807:
                raise ValueError("probe integer data is invalid")
            elif type(value) is float and (not math.isfinite(value) or value < 0.0):
                raise ValueError("probe float data is invalid")
            frozen[key] = value
    try:
        encoded = json.dumps(
            _thaw(frozen), sort_keys=True, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError):
        raise TypeError("probe data is not serializable") from None
    if len(encoded) > _MAX_REPORT_DATA_BYTES:
        raise ValueError("probe data is too large")
    return MappingProxyType(frozen)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


def _validate_name(name: object) -> None:
    if type(name) is not str or _NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("doctor probe name is invalid")


def _validate_code(code: object, *, prefix: str | None = None) -> None:
    if type(code) is not str or _CODE_PATTERN.fullmatch(code) is None:
        raise ValueError("doctor probe code is invalid")
    if prefix is not None and not code.startswith(prefix + "."):
        raise ValueError("doctor probe code does not match its probe")


def _validate_summary(summary: object) -> None:
    if (
        type(summary) is not str
        or not summary
        or summary.strip() != summary
        or len(summary.encode("utf-8")) > _MAX_SUMMARY_BYTES
        or any(ord(character) < 0x20 for character in summary)
        or _SENSITIVE_TEXT_PATTERN.search(summary) is not None
    ):
        raise ValueError("doctor probe summary is invalid")


def _validate_data_string(value: str) -> None:
    if (
        len(value.encode("utf-8")) > _MAX_DATA_STRING_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or _SECRET_VALUE_PATTERN.search(value) is not None
    ):
        raise ValueError("probe data string is invalid")


def _valid_generated_at(value: str) -> bool:
    if _TIMESTAMP_PATTERN.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is UTC or parsed.utcoffset() == UTC.utcoffset(None)


def _validate_report_path(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > 4_096
        or "\x00" in value
    ):
        raise ValueError("doctor project root is invalid")
    path = Path(value)
    if not path.is_absolute() or Path(os.path.normpath(value)) != path:
        raise ValueError("doctor project root is invalid")


def _generated_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _elapsed_ms(started: float) -> int:
    try:
        elapsed = int(max(0.0, time.monotonic() - started) * 1_000)
    except Exception:  # noqa: BLE001 - hostile clock boundary
        return 0
    return min(elapsed, _MAX_DURATION_MS)


__all__ = [
    "DEFAULT_PROBE_NAMES",
    "Doctor",
    "DoctorProbeInput",
    "DoctorProbeOutput",
    "DoctorReport",
    "DoctorStatus",
    "ProbeResult",
    "ProbeStatus",
    "build_doctor_registry",
]
