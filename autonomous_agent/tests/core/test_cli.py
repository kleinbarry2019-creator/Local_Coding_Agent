from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from autonomous_agent import cli
from autonomous_agent.core.config import (
    AgentConfig,
    CliOverrides,
    ConfigSource,
    ExecutionMode,
    FieldProvenance,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.doctor import (
    DoctorReport,
    DoctorStatus,
    ProbeResult,
    ProbeStatus,
)
from autonomous_agent.core.probes import ProbeError


def _config(
    tmp_path: Path,
    *,
    mode: ExecutionMode = ExecutionMode.MONITORED,
) -> AgentConfig:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    paths = ResolvedPaths(
        config_file=tmp_path / "config.toml",
        project_file=project / ".local-agent.toml",
        project_root=project,
        state_root=tmp_path / "state-does-not-exist",
    )
    return AgentConfig(
        schema_version=1,
        mode=mode,
        paths=paths,
        limits=ResourceLimits(),
        free_only=True,
        audit_required=True,
        provenance={
            "mode": FieldProvenance("mode", ConfigSource.CLI, None),
        },
    )


def _probe(
    name: str,
    *,
    status: ProbeStatus = ProbeStatus.PASS,
    required: bool = True,
) -> ProbeResult:
    return ProbeResult(
        name=name,
        status=status,
        required=required,
        code=f"{name}.ok",
        summary=f"{name} is ready.",
        data={},
        duration_ms=1,
        truncated=False,
    )


def _report(
    status: DoctorStatus = DoctorStatus.HEALTHY,
    *,
    mode: ExecutionMode = ExecutionMode.MONITORED,
    probes: tuple[ProbeResult, ...] | None = None,
) -> DoctorReport:
    selected = probes if probes is not None else (_probe("doctor.python"),)
    return DoctorReport(
        schema_version=1,
        status=status,
        generated_at="2026-08-24T00:00:00Z",
        mode=mode.value,
        free_only=True,
        project_root="/safe/project",
        probes=tuple(sorted(selected, key=lambda item: item.name)),
    )


def _fake_doctor(monkeypatch: pytest.MonkeyPatch, report: object) -> None:
    class FakeDoctor:
        def __init__(self, config: object, registry: object, names: object) -> None:
            assert config is not None
            assert registry is not None
            assert names == cli.DEFAULT_PROBE_NAMES

        def run(self) -> object:
            return report

    monkeypatch.setattr(cli, "build_doctor_registry", lambda config: object())
    monkeypatch.setattr(cli, "Doctor", FakeDoctor)


def test_help_exposes_only_phase_one_surface(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--help"]) == 0

    output = capsys.readouterr()
    assert "usage: agent" in output.out
    assert "doctor" in output.out
    assert output.err == ""

    parser = cli.build_parser()
    doctor_help = parser.parse_args(["doctor", "--mode", "monitored"])
    assert vars(doctor_help) == {
        "command": "doctor",
        "json": False,
        "project": None,
        "state_dir": None,
        "mode": ExecutionMode.MONITORED,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["run"],
        ["doctor", "--unknown"],
        ["doctor", "--mode", "unrestricted-root"],
        ["doctor", "--state-dir", "relative/state"],
        ["doctor", "--project", "relative/project"],
        ["doctor", "--project", "bad\npath"],
    ],
)
def test_invalid_arguments_return_two_without_process_exit_or_echo(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "agent: invalid command-line arguments.\n"
    assert "unrestricted-root" not in output.err
    assert "relative/state" not in output.err
    assert "relative/project" not in output.err


def test_doctor_passes_trusted_cli_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, mode=ExecutionMode.AUTONOMOUS)
    project = config.paths.project_root
    state = tmp_path / "explicit-state"
    seen: dict[str, object] = {}

    def fake_load_config(**kwargs: object) -> AgentConfig:
        seen.update(kwargs)
        return config

    monkeypatch.setattr(cli, "load_config", fake_load_config)
    _fake_doctor(
        monkeypatch,
        _report(DoctorStatus.HEALTHY, mode=ExecutionMode.AUTONOMOUS),
    )

    result = cli.main(
        [
            "doctor",
            "--project",
            str(project),
            "--state-dir",
            str(state),
            "--mode",
            "autonomous",
        ]
    )

    assert result == 0
    overrides = seen["cli"]
    assert overrides == CliOverrides(
        project_root=project,
        state_root=state,
        mode=ExecutionMode.AUTONOMOUS,
    )
    assert isinstance(seen["cwd"], Path)
    assert isinstance(seen["home"], Path)
    assert isinstance(seen["environ"], dict)
    assert capsys.readouterr().err == ""


def test_configured_unrestricted_root_is_rejected_before_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path, mode=ExecutionMode.UNRESTRICTED_ROOT)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    called = False

    def forbidden_registry(config: AgentConfig) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(cli, "build_doctor_registry", forbidden_registry)

    assert cli.main(["doctor"]) == 2
    assert called is False
    assert capsys.readouterr().err == "agent: configuration is invalid.\n"


def test_malformed_configuration_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-proj-this-must-never-be-printed"

    def fail(**kwargs: object) -> AgentConfig:
        raise cli.ConfigError("invalid_config", "config_file", secret)

    monkeypatch.setattr(cli, "load_config", fail)

    assert cli.main(["doctor"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "agent: configuration is invalid.\n"
    assert secret not in output.err


def test_declared_diagnostic_error_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: _config(tmp_path))

    def fail(config: AgentConfig) -> object:
        raise ProbeError("unsafe_environment", "secret path /private/value")

    monkeypatch.setattr(cli, "build_doctor_registry", fail)

    assert cli.main(["doctor"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "agent: diagnostics could not be initialized.\n"
    assert "/private/value" not in output.err


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DoctorStatus.HEALTHY, 0),
        (DoctorStatus.WARNING, 1),
        (DoctorStatus.UNHEALTHY, 1),
    ],
)
def test_report_status_controls_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: DoctorStatus,
    expected: int,
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: _config(tmp_path))
    _fake_doctor(monkeypatch, _report(status))

    assert cli.main(["doctor", "--json"]) == expected
    assert capsys.readouterr().err == ""


def test_json_is_exact_compact_sorted_report_with_one_newline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(
        DoctorStatus.WARNING,
        probes=(
            _probe("doctor.uv", status=ProbeStatus.WARNING, required=False),
            _probe("doctor.python"),
        ),
    )
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: _config(tmp_path))
    _fake_doctor(monkeypatch, report)

    assert cli.main(["doctor", "--json"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert (
        output.out
        == json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert json.loads(output.out)["schema_version"] == 1


def test_human_output_has_stable_groups_and_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(
        probes=(
            _probe("doctor.system"),
            _probe("doctor.git"),
            _probe("doctor.ollama-health", required=False),
            _probe("doctor.bubblewrap", required=False),
        ),
    )
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: _config(tmp_path))
    _fake_doctor(monkeypatch, report)

    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    headings = ["System:", "Development:", "Local model:", "Containment:", "Policy:"]
    assert [output.out.index(item) for item in headings] == sorted(
        output.out.index(item) for item in headings
    )
    assert "mode: monitored" in output.out
    assert "free-only: enabled" in output.out
    assert "/safe/project" not in output.out


@pytest.mark.parametrize("hostile_report", [True, 1, False, 0, None])
def test_hostile_non_report_is_internal_failure_without_bool_int_confusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hostile_report: object,
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: _config(tmp_path))
    _fake_doctor(monkeypatch, hostile_report)

    assert cli.main(["doctor", "--json"]) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "agent: internal diagnostic failure.\n"


def test_serialization_failure_is_redacted_and_emits_no_partial_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: _config(tmp_path))
    _fake_doctor(monkeypatch, _report())

    def fail(*args: object, **kwargs: object) -> str:
        raise ValueError("secret serialization detail")

    monkeypatch.setattr(cli.json, "dumps", fail)

    assert cli.main(["doctor", "--json"]) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "agent: internal diagnostic failure.\n"


def test_keyboard_interrupt_is_not_converted_to_internal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(**kwargs: object) -> AgentConfig:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "load_config", interrupt)

    with pytest.raises(KeyboardInterrupt):
        cli.main(["doctor"])


def test_doctor_path_has_no_state_imports_or_filesystem_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    state = tmp_path / "never-created-state"
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "Path", Path)
    _fake_doctor(monkeypatch, _report())

    assert cli.main(["doctor", "--state-dir", str(state), "--json"]) == 0

    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before
    assert not state.exists()
    assert capsys.readouterr().err == ""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import autonomous_agent.cli; "
                "assert 'autonomous_agent.core.state' not in sys.modules; "
                "assert 'autonomous_agent.core.events' not in sys.modules"
            ),
        ],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[3])},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_python_m_wires_main_without_initializing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fake_main(argv: object = None) -> int:
        calls.append(argv)
        return 1

    monkeypatch.setattr(cli, "main", fake_main)
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("autonomous_agent.__main__", run_name="__main__")

    assert raised.value.code == 1
    assert calls == [None]


def test_core_exports_only_stable_phase_one_contracts() -> None:
    from autonomous_agent import core

    assert set(core.__all__) == {
        "AgentConfig",
        "CliOverrides",
        "ConfigError",
        "DEFAULT_PROBE_NAMES",
        "Doctor",
        "DoctorReport",
        "DoctorStatus",
        "ExecutionMode",
        "ProbeResult",
        "ProbeStatus",
        "build_doctor_registry",
        "load_config",
    }
    assert not hasattr(core, "CoreStateStore")
    assert not hasattr(core, "AuditLog")
