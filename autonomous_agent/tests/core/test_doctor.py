from __future__ import annotations

import json
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from autonomous_agent.core.config import (
    AgentConfig,
    ExecutionMode,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.doctor import (
    DEFAULT_PROBE_NAMES,
    Doctor,
    DoctorProbeInput,
    DoctorProbeOutput,
    DoctorReport,
    DoctorStatus,
    ProbeResult,
    ProbeStatus,
    build_doctor_registry,
    canonicalize_doctor_report,
)
from autonomous_agent.core.policy import NetworkKind, SideEffect
from autonomous_agent.core.probes import LoopbackResponse, ProbeError, ProcessResult
from autonomous_agent.core.tools import (
    ExecutionContext,
    ToolRegistry,
    ToolSpec,
)


def _config(
    root: Path,
    *,
    state_root: Path | None = None,
    mode: ExecutionMode = ExecutionMode.MONITORED,
) -> AgentConfig:
    return AgentConfig(
        schema_version=1,
        mode=mode,
        paths=ResolvedPaths(
            config_file=root / "config.toml",
            project_file=root / ".local-agent.toml",
            project_root=root,
            state_root=state_root or root / "missing-state",
        ),
        limits=ResourceLimits(doctor_probe_timeout_s=1.0),
        free_only=True,
        audit_required=True,
        provenance=MappingProxyType({}),
    )


def _register(
    registry: ToolRegistry,
    name: str,
    handler: Callable[[DoctorProbeInput, ExecutionContext], DoctorProbeOutput],
    *,
    loopback: bool = False,
    max_output_bytes: int = 4_096,
) -> None:
    registry.register(
        ToolSpec(
            name=name,
            version="1.0.0",
            description="A controlled doctor test probe.",
            input_type=DoctorProbeInput,
            output_type=DoctorProbeOutput,
            capabilities=frozenset(
                {"doctor.ollama.loopback" if loopback else "doctor.read"}
            ),
            side_effect=SideEffect.READ_ONLY,
            network=(NetworkKind.LOOPBACK_DIAGNOSTIC if loopback else NetworkKind.NONE),
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=1.0,
            max_output_bytes=max_output_bytes,
            handler=handler,
        )
    )


def _output(
    *,
    status: ProbeStatus = ProbeStatus.PASS,
    code: str = "doctor.python.ok",
    summary: str = "Python is ready.",
    strings: dict[str, str] | None = None,
    integers: dict[str, int] | None = None,
    booleans: dict[str, bool] | None = None,
    truncated: bool = False,
) -> DoctorProbeOutput:
    return DoctorProbeOutput(
        status=status,
        code=code,
        summary=summary,
        strings={} if strings is None else strings,
        integers={} if integers is None else integers,
        booleans={} if booleans is None else booleans,
        floats={},
        string_lists={},
        truncated=truncated,
    )


def _snapshot(root: Path) -> tuple[tuple[str, bytes | None], ...]:
    return tuple(
        (
            str(path.relative_to(root)),
            path.read_bytes() if path.is_file() else None,
        )
        for path in sorted(root.rglob("*"))
    )


def test_import_is_stateless_and_does_not_load_storage_modules() -> None:
    sys.modules.pop("autonomous_agent.core.state", None)
    sys.modules.pop("autonomous_agent.core.events", None)

    module = sys.modules["autonomous_agent.core.doctor"]

    assert module is not None
    assert "autonomous_agent.core.state" not in sys.modules
    assert "autonomous_agent.core.events" not in sys.modules
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "core.state" not in source
    assert "core.events" not in source
    assert "subprocess" not in source
    assert "socket" not in source
    assert "urlopen" not in source
    assert "requests" not in source


def test_healthy_report_is_versioned_ordered_immutable_and_copy_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "autonomous_agent.core.doctor._generated_at",
        lambda: "2026-08-23T12:34:56Z",
    )
    registry = ToolRegistry()
    caller_data = {"version": "3.14.0"}
    _register(
        registry,
        "doctor.python",
        lambda _request, _context: _output(strings=caller_data),
    )

    report = Doctor(_config(tmp_path), registry, ("doctor.python",)).run()
    caller_data["version"] = "mutated"

    assert report.status is DoctorStatus.HEALTHY
    assert report.probes[0].status is ProbeStatus.PASS
    assert report.probes[0].data == {"version": "3.14.0"}
    assert report.to_dict() == {
        "schema_version": 1,
        "status": "healthy",
        "generated_at": "2026-08-23T12:34:56Z",
        "mode": "monitored",
        "free_only": True,
        "project_root": str(tmp_path),
        "probes": [
            {
                "name": "doctor.python",
                "status": "pass",
                "required": True,
                "code": "doctor.python.ok",
                "summary": "Python is ready.",
                "data": {"version": "3.14.0"},
                "duration_ms": report.probes[0].duration_ms,
                "truncated": False,
            }
        ],
    }
    assert json.dumps(report.to_dict(), sort_keys=True)
    with pytest.raises(FrozenInstanceError):
        report.status = DoctorStatus.WARNING  # type: ignore[misc]
    with pytest.raises(TypeError):
        report.probes[0].data["version"] = "unsafe"  # type: ignore[index]


def test_probe_order_is_stable_independent_of_requested_order(tmp_path: Path) -> None:
    registry = ToolRegistry()
    for name in ("doctor.uv", "doctor.python", "doctor.gh"):
        _register(
            registry,
            name,
            lambda _request, _context, probe=name: _output(
                code=f"{probe}.ok", summary="Probe is ready."
            ),
        )

    report = Doctor(
        _config(tmp_path),
        registry,
        ("doctor.uv", "doctor.python", "doctor.gh"),
    ).run()

    assert tuple(item.name for item in report.probes) == (
        "doctor.gh",
        "doctor.python",
        "doctor.uv",
    )


@pytest.mark.parametrize(
    ("name", "handler", "status", "probe_status", "code"),
    [
        (
            "doctor.gh",
            lambda _request, _context: _output(
                status=ProbeStatus.WARNING,
                code="doctor.gh.unavailable",
                summary="GitHub CLI is unavailable.",
            ),
            DoctorStatus.WARNING,
            ProbeStatus.WARNING,
            "doctor.gh.unavailable",
        ),
        (
            "doctor.python",
            lambda _request, _context: _output(
                status=ProbeStatus.FAIL,
                code="doctor.python.unavailable",
                summary="Python is unavailable.",
            ),
            DoctorStatus.UNHEALTHY,
            ProbeStatus.FAIL,
            "doctor.python.unavailable",
        ),
        (
            "doctor.uv",
            lambda _request, _context: _output(truncated=True),
            DoctorStatus.WARNING,
            ProbeStatus.WARNING,
            "doctor.uv.truncated",
        ),
    ],
)
def test_status_derivation_and_stable_codes(
    tmp_path: Path,
    name: str,
    handler: Callable[[DoctorProbeInput, ExecutionContext], DoctorProbeOutput],
    status: DoctorStatus,
    probe_status: ProbeStatus,
    code: str,
) -> None:
    registry = ToolRegistry()
    _register(registry, name, handler)

    report = Doctor(_config(tmp_path), registry, (name,)).run()

    assert report.status is status
    assert report.probes[0].status is probe_status
    assert report.probes[0].code == code


def test_timeout_is_distinct_and_required_timeout_is_unhealthy(tmp_path: Path) -> None:
    registry = ToolRegistry()

    def too_slow(
        _request: DoctorProbeInput, context: ExecutionContext
    ) -> DoctorProbeOutput:
        while time.monotonic() < context.deadline_monotonic + 0.01:
            pass
        return _output()

    _register(registry, "doctor.python", too_slow)

    report = Doctor(_config(tmp_path), registry, ("doctor.python",)).run()

    assert report.status is DoctorStatus.UNHEALTHY
    assert report.probes[0].status is ProbeStatus.FAIL
    assert report.probes[0].code == "doctor.python.timeout"
    assert report.probes[0].truncated is False


@pytest.mark.parametrize(
    ("name", "overall", "probe_status"),
    [
        ("doctor.python", DoctorStatus.UNHEALTHY, ProbeStatus.FAIL),
        ("doctor.uv", DoctorStatus.WARNING, ProbeStatus.WARNING),
    ],
)
def test_registry_output_truncation_is_classified_before_generic_failure(
    tmp_path: Path,
    name: str,
    overall: DoctorStatus,
    probe_status: ProbeStatus,
) -> None:
    registry = ToolRegistry()
    _register(
        registry,
        name,
        lambda _request, _context: _output(
            code=f"{name}.ok",
            summary="The oversized diagnostic completed.",
            strings={"version": "x" * 400},
        ),
        max_output_bytes=256,
    )

    report = Doctor(_config(tmp_path), registry, (name,)).run()

    assert report.status is overall
    assert report.probes[0].status is probe_status
    assert report.probes[0].code == f"{name}.truncated"
    assert report.probes[0].truncated is True
    assert report.probes[0].data == {}


def test_unexpected_exception_becomes_redacted_in_memory_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-proj-secret-must-not-escape"
    registry = ToolRegistry()
    _register(
        registry,
        "doctor.python",
        lambda _request, _context: (_ for _ in ()).throw(
            RuntimeError(f"TOKEN={secret}\ntraceback raw-output")
        ),
    )
    monkeypatch.setenv("LOCAL_AGENT_API_TOKEN", secret)

    report = Doctor(_config(tmp_path), registry, ("doctor.python",)).run()
    rendered = json.dumps(report.to_dict(), sort_keys=True)

    assert report.status is DoctorStatus.UNHEALTHY
    assert report.probes[0].code == "doctor.python.internal_error"
    assert set(report.probes[0].data) == {"incident_id"}
    assert secret not in rendered
    assert "traceback" not in rendered.lower()
    assert "raw-output" not in rendered
    assert "LOCAL_AGENT_API_TOKEN" not in rendered


def test_registry_accessor_exception_is_isolated_without_raw_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ghp_1234567890abcdef"
    registry = ToolRegistry()

    def explode(_name: str, _raw: object, _context: ExecutionContext) -> object:
        raise RuntimeError(f"password={secret}\nraw argv traceback")

    monkeypatch.setattr(registry, "execute", explode)

    report = Doctor(_config(tmp_path), registry, ("doctor.python",)).run()
    rendered = json.dumps(report.to_dict(), sort_keys=True)

    assert report.probes[0].code == "doctor.python.internal_error"
    assert secret not in rendered
    assert "raw argv" not in rendered
    assert "traceback" not in rendered


def test_invalid_nested_data_and_bool_as_int_fail_closed(tmp_path: Path) -> None:
    registry = ToolRegistry()
    _register(
        registry,
        "doctor.resources",
        lambda _request, _context: cast(
            DoctorProbeOutput,
            object.__new__(DoctorProbeOutput),
        ),
    )
    hostile = registry._specs["doctor.resources"]  # type: ignore[attr-defined]
    forged = object.__new__(DoctorProbeOutput)
    object.__setattr__(forged, "status", ProbeStatus.PASS)
    object.__setattr__(forged, "code", "doctor.resources.ok")
    object.__setattr__(forged, "summary", "Resources are ready.")
    object.__setattr__(forged, "strings", {})
    object.__setattr__(forged, "integers", {"cpu_count": True})
    object.__setattr__(forged, "booleans", {})
    object.__setattr__(forged, "floats", {})
    object.__setattr__(forged, "string_lists", {})
    object.__setattr__(forged, "truncated", False)
    object.__setattr__(hostile, "handler", lambda _request, _context: forged)

    report = Doctor(_config(tmp_path), registry, ("doctor.resources",)).run()

    assert report.status is DoctorStatus.WARNING
    assert report.probes[0].code == "doctor.resources.internal_error"
    assert set(report.probes[0].data) == {"incident_id"}


def test_default_inventory_is_complete_and_registry_classification_is_exact(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    registry = build_doctor_registry(config)

    assert DEFAULT_PROBE_NAMES == tuple(sorted(DEFAULT_PROBE_NAMES))
    assert {
        "doctor.python",
        "doctor.sqlite",
        "doctor.git",
        "doctor.project",
        "doctor.state-path",
        "doctor.system",
        "doctor.resources",
        "doctor.gh",
        "doctor.ollama-health",
        "doctor.ollama-tags",
        "doctor.nvidia",
        "doctor.podman",
        "doctor.bubblewrap",
        "doctor.systemd-run",
        "doctor.node",
        "doctor.npm",
        "doctor.uv",
        "doctor.quality",
        "doctor.runtime-paths",
    } == set(DEFAULT_PROBE_NAMES)
    for name, spec in registry._specs.items():  # type: ignore[attr-defined]
        assert name in DEFAULT_PROBE_NAMES
        assert spec.side_effect is SideEffect.READ_ONLY
        assert spec.requires_elevation is False
        assert spec.requires_recovery is False
        assert spec.capabilities in {
            frozenset({"doctor.read"}),
            frozenset({"doctor.ollama.loopback"}),
        }


def test_quality_inventory_resolves_tools_without_executing_wrappers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "executed-marker"
    wrappers: dict[str, Path] = {}
    for name in ("bandit", "ruff"):
        wrapper = tmp_path / name
        wrapper.write_text(f"#!/bin/sh\nprintf unsafe > {marker}\n", encoding="utf-8")
        wrapper.chmod(0o755)
        wrappers[name] = wrapper

    class ControlledResolver:
        def resolve(self, name: str) -> Path:
            if name in wrappers:
                return wrappers[name]
            raise ProbeError(
                "executable_not_found", "trusted executable is unavailable"
            )

    command_calls: list[str] = []

    def forbidden_command(
        name: str,
        _arguments: tuple[str, ...],
        _context: ExecutionContext,
    ) -> ProcessResult:
        command_calls.append(name)
        raise AssertionError("quality discovery executed a command")

    monkeypatch.setattr(
        "autonomous_agent.core.doctor.TrustedExecutableResolver",
        ControlledResolver,
    )
    monkeypatch.setattr("autonomous_agent.core.doctor._command", forbidden_command)
    config = _config(tmp_path)

    report = Doctor(config, build_doctor_registry(config), ("doctor.quality",)).run()

    assert report.status is DoctorStatus.WARNING
    assert report.probes[0].code == "doctor.quality.partial"
    assert report.probes[0].data == {
        "available": ("bandit", "ruff"),
        "bandit_path": str(wrappers["bandit"]),
        "missing": ("mypy", "pytest"),
        "ruff_path": str(wrappers["ruff"]),
    }
    assert command_calls == []
    assert not marker.exists()


def test_default_doctor_has_no_persistent_side_effects(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = project / "tracked.txt"
    marker.write_text("stable", encoding="utf-8")
    state_root = tmp_path / "state-does-not-exist"
    config = _config(project, state_root=state_root)
    before = _snapshot(tmp_path)

    report = Doctor(
        config,
        build_doctor_registry(config),
        (
            "doctor.project",
            "doctor.state-path",
            "doctor.runtime-paths",
            "doctor.sqlite",
        ),
    ).run()

    assert report.status in set(DoctorStatus)
    assert _snapshot(tmp_path) == before
    assert not state_root.exists()
    assert not tuple(tmp_path.rglob("*.db"))
    assert not tuple(tmp_path.rglob("*-wal"))
    assert not tuple(tmp_path.rglob("audit*"))
    assert marker.read_text(encoding="utf-8") == "stable"


def test_every_default_handler_produces_a_bounded_schema_valid_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _config(project, state_root=tmp_path / "missing-state")

    def fake_command(
        name: str,
        arguments: tuple[str, ...],
        _context: ExecutionContext,
    ) -> ProcessResult:
        if name == "git" and "rev-parse" in arguments:
            output = b"true\n"
        elif name == "git" and "status" in arguments:
            output = b""
        elif name == "nvidia-smi":
            output = b"RTX 2070 SUPER, 8192, 6144, 555.42\n"
        else:
            output = f"{name} 1.2.3\n".encode()
        return ProcessResult(
            returncode=0,
            stdout=output,
            stderr=b"",
            timed_out=False,
            truncated=False,
            duration_ms=1,
        )

    def fake_loopback(
        _endpoint: str | Path,
        request_path: str,
        _deadline: float,
        _max_bytes: int,
    ) -> LoopbackResponse:
        data: object = (
            {"version": "1.2.3"}
            if request_path == "/api/version"
            else {"models": [{"name": "qwen:7b"}, {"name": "qwen:7b"}]}
        )
        return LoopbackResponse(status_code=200, data=data, body_bytes=64)

    class AllPresentResolver:
        def resolve(self, name: str) -> Path:
            return Path("/usr/bin") / name

    monkeypatch.setattr("autonomous_agent.core.doctor._command", fake_command)
    monkeypatch.setattr(
        "autonomous_agent.core.doctor.TrustedExecutableResolver",
        AllPresentResolver,
    )
    monkeypatch.setattr("autonomous_agent.core.doctor.get_loopback_json", fake_loopback)

    def forbidden_mkdtemp(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("Doctor called mkdtemp")

    def forbidden_mkdir(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Doctor called mkdir")

    monkeypatch.setattr(tempfile, "mkdtemp", forbidden_mkdtemp)
    monkeypatch.setattr(Path, "mkdir", forbidden_mkdir)

    report = Doctor(config, build_doctor_registry(config), DEFAULT_PROBE_NAMES).run()

    assert report.status is DoctorStatus.HEALTHY
    assert len(report.probes) == len(DEFAULT_PROBE_NAMES)
    assert all(item.status is ProbeStatus.PASS for item in report.probes)
    serialized = cast(list[dict[str, object]], report.to_dict()["probes"])
    assert all(len(json.dumps(item["data"])) <= 8_192 for item in serialized)
    ollama_tags = next(
        item for item in report.probes if item.name == "doctor.ollama-tags"
    )
    assert ollama_tags.data["models"] == ("qwen:7b",)


def test_public_models_reject_unbounded_or_wrong_typed_values() -> None:
    with pytest.raises(ValueError, match="summary"):
        ProbeResult(
            name="doctor.python",
            status=ProbeStatus.PASS,
            required=True,
            code="doctor.python.ok",
            summary="x" * 300,
            data={},
            duration_ms=0,
            truncated=False,
        )
    with pytest.raises(TypeError, match="duration"):
        ProbeResult(
            name="doctor.python",
            status=ProbeStatus.PASS,
            required=True,
            code="doctor.python.ok",
            summary="Python is ready.",
            data={},
            duration_ms=cast(int, True),
            truncated=False,
        )
    with pytest.raises(TypeError, match="free_only"):
        DoctorReport(
            schema_version=1,
            status=DoctorStatus.HEALTHY,
            generated_at="2026-08-23T12:34:56Z",
            mode="monitored",
            free_only=cast(bool, 1),
            project_root="/safe",
            probes=(),
        )
    with pytest.raises(ValueError, match="string"):
        ProbeResult(
            name="doctor.python",
            status=ProbeStatus.PASS,
            required=True,
            code="doctor.python.ok",
            summary="Python is ready.",
            data={"version": "token=ghp_1234567890abcdef"},
            duration_ms=0,
            truncated=False,
        )


def test_report_canonical_copy_and_serialization_revalidate_nested_results() -> None:
    probe = ProbeResult(
        name="doctor.python",
        status=ProbeStatus.PASS,
        required=True,
        code="doctor.python.ok",
        summary="Python is ready.",
        data={"version": "3.14.6", "supported": True},
        duration_ms=1,
        truncated=False,
    )
    report = DoctorReport(
        schema_version=1,
        status=DoctorStatus.HEALTHY,
        generated_at="2026-08-24T00:00:00Z",
        mode="monitored",
        free_only=True,
        project_root="/safe",
        probes=(probe,),
    )
    object.__setattr__(probe, "data", {"version": "LEAKED_SECRET", "supported": True})

    with pytest.raises(ValueError, match="string"):
        canonicalize_doctor_report(report)
    with pytest.raises(ValueError, match="string"):
        report.to_dict()


def test_exact_report_serializer_ignores_shadowed_instance_callables() -> None:
    probe = ProbeResult(
        name="doctor.python",
        status=ProbeStatus.PASS,
        required=True,
        code="doctor.python.ok",
        summary="Python is ready.",
        data={"version": "3.14.6", "supported": True},
        duration_ms=1,
        truncated=False,
    )
    report = DoctorReport(
        schema_version=1,
        status=DoctorStatus.HEALTHY,
        generated_at="2026-08-24T00:00:00Z",
        mode="monitored",
        free_only=True,
        project_root="/safe",
        probes=(probe,),
    )
    object.__setattr__(probe, "data", {"version": "LEAKED_SECRET", "supported": True})
    object.__setattr__(report, "canonical_copy", lambda: report)
    object.__setattr__(report, "to_dict", lambda: {"raw": "LEAKED_SECRET"})

    with pytest.raises(ValueError, match="string"):
        DoctorReport.to_dict(report)
