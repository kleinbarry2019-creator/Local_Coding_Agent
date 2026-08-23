"""Installable command-line boundary for the phase-1 local agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from autonomous_agent.core.config import (
    AgentConfig,
    CliOverrides,
    ConfigError,
    ExecutionMode,
    load_config,
)
from autonomous_agent.core.doctor import (
    DEFAULT_PROBE_NAMES,
    Doctor,
    DoctorReport,
    DoctorStatus,
    ProbeResult,
    build_doctor_registry,
)
from autonomous_agent.core.probes import ProbeError

_INVALID_ARGUMENTS = "agent: invalid command-line arguments."
_INVALID_CONFIGURATION = "agent: configuration is invalid."
_DIAGNOSTIC_ERROR = "agent: diagnostics could not be initialized."
_INTERNAL_ERROR = "agent: internal diagnostic failure."
_MAX_PATH_BYTES = 4_096

_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "System",
        frozenset(
            {
                "doctor.python",
                "doctor.resources",
                "doctor.sqlite",
                "doctor.system",
            }
        ),
    ),
    (
        "Development",
        frozenset(
            {
                "doctor.gh",
                "doctor.git",
                "doctor.node",
                "doctor.npm",
                "doctor.project",
                "doctor.quality",
                "doctor.uv",
            }
        ),
    ),
    (
        "Local model",
        frozenset(
            {
                "doctor.nvidia",
                "doctor.ollama-health",
                "doctor.ollama-tags",
            }
        ),
    ),
    (
        "Containment",
        frozenset(
            {
                "doctor.bubblewrap",
                "doctor.podman",
                "doctor.runtime-paths",
                "doctor.state-path",
                "doctor.systemd-run",
            }
        ),
    ),
)


class _CliArgumentError(Exception):
    """A deliberately detail-free argparse boundary failure."""


class _ConfigurationBoundaryError(Exception):
    """An effective configuration that violates the phase-1 CLI contract."""


class _AgentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CliArgumentError


def build_parser() -> argparse.ArgumentParser:
    """Build the complete and intentionally small phase-1 parser."""
    parser = _AgentArgumentParser(
        prog="agent",
        description="Free, local-first coding agent diagnostics.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor",
        help="run stateless local readiness diagnostics",
        description="Run stateless, read-only local readiness diagnostics.",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="emit compact schema-version-1 JSON",
    )
    doctor.add_argument(
        "--project",
        type=_project_path,
        metavar="PATH",
        help="use an explicit project root",
    )
    doctor.add_argument(
        "--state-dir",
        type=_state_path,
        metavar="PATH",
        help="inspect an explicit absolute state path without creating it",
    )
    doctor.add_argument(
        "--mode",
        type=_execution_mode,
        metavar="{monitored,autonomous}",
        help="select the trusted diagnostic policy mode",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI without terminating the caller process."""
    parser = build_parser()
    try:
        arguments = None if argv is None else list(argv)
        if arguments is not None and any(type(item) is not str for item in arguments):
            raise _CliArgumentError
        namespace = parser.parse_args(arguments)
    except _CliArgumentError:
        _write_error(_INVALID_ARGUMENTS)
        return 2
    except SystemExit as error:
        # argparse uses SystemExit only for its successful help action because
        # parser failures are converted to _CliArgumentError above.
        return 0 if error.code == 0 else 2

    try:
        if namespace.command != "doctor":
            raise _CliArgumentError
        config = load_config(
            cwd=Path.cwd(),
            home=Path.home(),
            environ=dict(os.environ),
            cli=CliOverrides(
                project_root=namespace.project,
                state_root=namespace.state_dir,
                mode=namespace.mode,
            ),
        )
        _validate_effective_config(config)
        registry = build_doctor_registry(config)
        report = Doctor(config, registry, DEFAULT_PROBE_NAMES).run()
        _validate_report(report)
        rendered = _render_json(report) if namespace.json else _render_human(report)
        exit_code = 0 if report.status is DoctorStatus.HEALTHY else 1
    except _CliArgumentError:
        _write_error(_INVALID_ARGUMENTS)
        return 2
    except (ConfigError, _ConfigurationBoundaryError):
        _write_error(_INVALID_CONFIGURATION)
        return 2
    except ProbeError:
        _write_error(_DIAGNOSTIC_ERROR)
        return 2
    except Exception:  # noqa: BLE001 - final redacted process boundary
        _write_error(_INTERNAL_ERROR)
        return 3

    try:
        sys.stdout.write(rendered)
    except Exception:  # noqa: BLE001 - redacted output boundary
        _write_error(_INTERNAL_ERROR)
        return 3
    return exit_code


def _project_path(value: str) -> Path:
    return _validated_cli_path(value, require_absolute=True)


def _state_path(value: str) -> Path:
    return _validated_cli_path(value, require_absolute=True)


def _validated_cli_path(value: str, *, require_absolute: bool) -> Path:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise argparse.ArgumentTypeError("invalid path")
    path = Path(value)
    if require_absolute and (
        not path.is_absolute() or Path(os.path.normpath(value)) != path
    ):
        raise argparse.ArgumentTypeError("invalid path")
    return path


def _execution_mode(value: str) -> ExecutionMode:
    if value == ExecutionMode.MONITORED.value:
        return ExecutionMode.MONITORED
    if value == ExecutionMode.AUTONOMOUS.value:
        return ExecutionMode.AUTONOMOUS
    raise argparse.ArgumentTypeError("invalid mode")


def _validate_effective_config(config: object) -> None:
    if type(config) is not AgentConfig:
        raise TypeError("invalid effective configuration")
    if config.mode not in {ExecutionMode.MONITORED, ExecutionMode.AUTONOMOUS}:
        raise _ConfigurationBoundaryError
    if config.free_only is not True:
        raise _ConfigurationBoundaryError


def _validate_report(report: object) -> None:
    if type(report) is not DoctorReport:
        raise TypeError("invalid doctor report")
    # Revalidate the immutable value at the CLI trust boundary so hostile
    # monkeypatches/object mutation cannot exploit bool/int equality.
    DoctorReport(
        schema_version=report.schema_version,
        status=report.status,
        generated_at=report.generated_at,
        mode=report.mode,
        free_only=report.free_only,
        project_root=report.project_root,
        probes=report.probes,
    )
    if (
        report.mode
        not in {
            ExecutionMode.MONITORED.value,
            ExecutionMode.AUTONOMOUS.value,
        }
        or report.free_only is not True
    ):
        raise TypeError("invalid doctor report policy")


def _render_json(report: DoctorReport) -> str:
    return json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def _render_human(report: DoctorReport) -> str:
    lines = [f"Agent doctor: {report.status.value}"]
    assigned: set[str] = set()
    for title, names in _GROUPS:
        lines.append(f"{title}:")
        for probe in report.probes:
            if probe.name in names:
                lines.append(_probe_line(probe))
                assigned.add(probe.name)

    # Future schema-approved diagnostics remain visible without changing the
    # stable heading order. Current phase-1 reports never use this fallback.
    unexpected = [probe for probe in report.probes if probe.name not in assigned]
    if unexpected:
        development_index = lines.index("Development:") + 1
        lines[development_index:development_index] = [
            _probe_line(probe) for probe in unexpected
        ]

    lines.extend(
        (
            "Policy:",
            f"  [PASS] mode: {report.mode}",
            "  [PASS] free-only: enabled",
        )
    )
    return "\n".join(lines) + "\n"


def _probe_line(probe: ProbeResult) -> str:
    return f"  [{probe.status.value.upper()}] {probe.name}: {probe.summary}"


def _write_error(message: str) -> None:
    sys.stderr.write(message + "\n")


__all__ = ["build_parser", "main"]
