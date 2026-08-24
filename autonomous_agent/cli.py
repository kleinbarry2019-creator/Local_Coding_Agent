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
    canonicalize_doctor_report,
)
from autonomous_agent.core.goals import GoalError
from autonomous_agent.core.probes import ProbeError

_CLI_NAME = "acb"
_DISPLAY_NAME = "ACB – Autonome Computing Butler"
_INVALID_ARGUMENTS = f"{_CLI_NAME}: invalid command-line arguments."
_INVALID_CONFIGURATION = f"{_CLI_NAME}: configuration is invalid."
_DIAGNOSTIC_ERROR = f"{_CLI_NAME}: diagnostics could not be initialized."
_INTERNAL_ERROR = f"{_CLI_NAME}: internal diagnostic failure."
_RUNTIME_ERROR = f"{_CLI_NAME}: task execution failed before verification."
_UI_ERROR = f"{_CLI_NAME}: graphical interface could not be started."
_APP_ERROR = f"{_CLI_NAME}: desktop application could not be started."
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


class _ParserExit(Exception):
    """Private argparse control flow that cannot be confused with SystemExit."""

    def __init__(self, status: int) -> None:
        self.status = status if type(status) is int and status >= 0 else 2
        super().__init__(self.status)


class _AgentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CliArgumentError

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        del message
        raise _ParserExit(status)


def build_parser() -> argparse.ArgumentParser:
    """Build the complete and intentionally small phase-1 parser."""
    parser = _AgentArgumentParser(
        prog=_CLI_NAME,
        description=f"{_DISPLAY_NAME}: free, local-first coding and system agent.",
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
    run = commands.add_parser(
        "run",
        help="execute and verify one autonomous local task",
        description="Execute a bounded local task and require completion evidence.",
    )
    run.add_argument("goal", help="simple coding or system task in natural language")
    run.add_argument("--json", action="store_true", help="emit compact JSON")
    run.add_argument("--project", type=_project_path, metavar="PATH")
    run.add_argument("--state-dir", type=_state_path, metavar="PATH")
    resume = commands.add_parser(
        "resume",
        help="resume or inspect a persisted autonomous task",
    )
    resume.add_argument("session_id", type=_session_id, metavar="SESSION")
    resume.add_argument("--json", action="store_true", help="emit compact JSON")
    resume.add_argument("--project", type=_project_path, metavar="PATH")
    resume.add_argument("--state-dir", type=_state_path, metavar="PATH")
    ui = commands.add_parser(
        "ui",
        help="start the local browser interface",
        description="Start the loopback-only ACB browser interface.",
    )
    ui.add_argument("--project", type=_project_path, metavar="PATH")
    ui.add_argument("--state-dir", type=_state_path, metavar="PATH")
    ui.add_argument("--host", type=_ui_host, default="127.0.0.1", metavar="HOST")
    ui.add_argument(
        "--port",
        type=_ui_port,
        default=8765,
        metavar="PORT",
        help="loopback port (default: 8765)",
    )
    ui.add_argument(
        "--open",
        action="store_true",
        help="open the interface in the default browser",
    )
    app = commands.add_parser(
        "app",
        help="start the offline desktop application",
        description="Start the installed, offline-capable ACB desktop application.",
    )
    app.add_argument("--project", type=_project_path, metavar="PATH")
    app.add_argument("--state-dir", type=_state_path, metavar="PATH")
    learn = commands.add_parser(
        "learn",
        help="research trusted AI and security feeds",
        description="Research allow-listed feeds and store gated local proposals.",
    )
    learn.add_argument("--network", action="store_true", help="allow bounded HTTPS feed access")
    learn.add_argument("--json", action="store_true", help="emit compact JSON")
    learn.add_argument("--project", type=_project_path, metavar="PATH")
    learn.add_argument("--state-dir", type=_state_path, metavar="PATH")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI without terminating the caller process."""
    runtime_command = False
    try:
        parser = build_parser()
        arguments = None if argv is None else list(argv)
    except Exception:  # noqa: BLE001 - redacted parser preparation boundary
        _write_error(_INTERNAL_ERROR)
        return 3

    if arguments is not None and any(type(item) is not str for item in arguments):
        _write_error(_INVALID_ARGUMENTS)
        return 2

    try:
        namespace = parser.parse_args(arguments)
    except _CliArgumentError:
        _write_error(_INVALID_ARGUMENTS)
        return 2
    except _ParserExit as error:
        if error.status == 0:
            return 0
        _write_error(_INVALID_ARGUMENTS)
        return 2
    except Exception:  # noqa: BLE001 - redacted parser execution boundary
        _write_error(_INTERNAL_ERROR)
        return 3

    try:
        is_doctor = namespace.command == "doctor"
        is_runtime = namespace.command in {"run", "resume", "ui", "app", "learn"}
        runtime_command = is_runtime
        if not is_doctor and not is_runtime:
            raise _CliArgumentError
        config = load_config(
            cwd=Path.cwd(),
            # Bazzite exposes the account home through the lexical /home
            # compatibility symlink. Pass the canonical location so the
            # config/state resolver can retain its no-symlink invariant.
            home=Path.home().resolve(strict=False),
            environ=dict(os.environ),
            cli=CliOverrides(
                project_root=namespace.project,
                state_root=namespace.state_dir,
                mode=(
                    namespace.mode
                    if is_doctor
                    else ExecutionMode.AUTONOMOUS
                ),
            ),
        )
        _validate_effective_config(config)
        if namespace.command == "app":
            from autonomous_agent.app import launch

            return launch(config)
        if namespace.command == "learn":
            from autonomous_agent.core.learning import LearningService

            service = LearningService(
                config.paths.state_root,
                config.paths.project_root,
                network_enabled=namespace.network,
            )
            research = service.research_now()
            if namespace.json:
                rendered = json.dumps(research, ensure_ascii=False, separators=(",", ":")) + "\n"
            else:
                rendered = _render_learning_human(research)
            return_code = 0 if research.get("status") in {"ok", "offline", "partial"} else 1
            sys.stdout.write(rendered)
            return return_code
        if namespace.command == "ui":
            import webbrowser

            from autonomous_agent.ui import AcbUiServer

            server = AcbUiServer(
                config,
                host=namespace.host,
                port=namespace.port,
            )
            if namespace.open:
                webbrowser.open(server.url)
            sys.stdout.write(
                f"{_DISPLAY_NAME} UI: {server.url}\n"
                "Stop with Ctrl+C.\n"
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.shutdown()
            return 0
        if is_doctor:
            registry = build_doctor_registry(config)
            raw_report = Doctor(config, registry, DEFAULT_PROBE_NAMES).run()
            report = _canonical_report(raw_report)
            rendered = _render_json(report) if namespace.json else _render_human(report)
            exit_code = 0 if report.status is DoctorStatus.HEALTHY else 1
        else:
            # Keep doctor strictly zero-write and free of state imports.
            from autonomous_agent.core.autonomy import AutonomyRuntime

            runtime = AutonomyRuntime(config)
            result = (
                runtime.run(namespace.goal)
                if namespace.command == "run"
                else runtime.resume(namespace.session_id)
            )
            rendered = (
                _render_runtime_json(result)
                if namespace.json
                else _render_runtime_human(result)
            )
            exit_code = 0 if result.completion.completed else 1
    except _CliArgumentError:
        _write_error(_INVALID_ARGUMENTS)
        return 2
    except (ConfigError, _ConfigurationBoundaryError):
        _write_error(_INVALID_CONFIGURATION)
        return 2
    except ProbeError:
        _write_error(_DIAGNOSTIC_ERROR)
        return 2
    except GoalError:
        _write_error(_INVALID_ARGUMENTS)
        return 2
    except (ValueError, RuntimeError):
        if namespace.command == "ui":
            _write_error(_UI_ERROR)
        elif namespace.command == "app":
            _write_error(_APP_ERROR)
        else:
            _write_error(_RUNTIME_ERROR if runtime_command else _INTERNAL_ERROR)
        return 3
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


def _session_id(value: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("session-")
        or len(value) > 128
        or not all(character.isalnum() or character == "-" for character in value)
    ):
        raise argparse.ArgumentTypeError("invalid session")
    return value


def _ui_host(value: str) -> str:
    from autonomous_agent.ui import validate_ui_host

    try:
        return validate_ui_host(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid loopback host") from error


def _ui_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("invalid port") from error
    if not 0 <= port <= 65_535:
        raise argparse.ArgumentTypeError("invalid port")
    return port


def _validate_effective_config(config: object) -> None:
    if type(config) is not AgentConfig:
        raise TypeError("invalid effective configuration")
    if config.mode not in {ExecutionMode.MONITORED, ExecutionMode.AUTONOMOUS}:
        raise _ConfigurationBoundaryError
    if config.free_only is not True:
        raise _ConfigurationBoundaryError


def _canonical_report(report: object) -> DoctorReport:
    canonical = canonicalize_doctor_report(report)
    if (
        canonical.mode
        not in {
            ExecutionMode.MONITORED.value,
            ExecutionMode.AUTONOMOUS.value,
        }
        or canonical.free_only is not True
    ):
        raise TypeError("invalid doctor report policy")
    return canonical


def _render_json(report: DoctorReport) -> str:
    document = DoctorReport.to_dict(report)
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


def _render_human(report: DoctorReport) -> str:
    lines = [f"{_DISPLAY_NAME} doctor: {report.status.value}"]
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


def _render_runtime_json(result: object) -> str:
    from autonomous_agent.core.autonomy import RuntimeResult

    if type(result) is not RuntimeResult:
        raise TypeError("runtime result is invalid")
    return json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def _render_runtime_human(result: object) -> str:
    from autonomous_agent.core.autonomy import RuntimeResult

    if type(result) is not RuntimeResult:
        raise TypeError("runtime result is invalid")
    lines = [
        f"{_DISPLAY_NAME} task: {result.status}",
        f"Session: {result.session_id}",
        f"Executed: {'yes' if result.completion.executed else 'no'}",
        f"Tested: {'yes' if result.completion.tested else 'no'}",
        f"E2E verified: {'yes' if result.completion.e2e_verified else 'no'}",
        "Acceptance criteria:",
    ]
    lines.extend(
        f"  [{'PASS' if item.passed else 'FAIL'}] {item.criterion_id}: {item.evidence}"
        for item in result.completion.criteria
    )
    return "\n".join(lines) + "\n"


def _render_learning_human(result: object) -> str:
    document = result if isinstance(result, dict) else {}
    status = document.get("status", "unknown")
    items = document.get("items", 0)
    errors = document.get("errors", [])
    suffix = (
        f"; nicht verfügbar: {', '.join(str(item) for item in errors)}"
        if isinstance(errors, list) and errors
        else ""
    )
    return f"{_DISPLAY_NAME} Lernen: {status}\nNeue Wissenseinträge: {items}{suffix}\n"


def _probe_line(probe: ProbeResult) -> str:
    return f"  [{probe.status.value.upper()}] {probe.name}: {probe.summary}"


def _write_error(message: str) -> None:
    sys.stderr.write(message + "\n")


__all__ = ["build_parser", "main"]
