"""Hardened, local-only primitives used by stateless doctor probes."""

from __future__ import annotations

import errno
import http.client
import ipaddress
import json
import math
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess  # nosec B404
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Self, cast

from autonomous_agent.core.config import ConfigError

_SYSTEM_EXECUTABLE_ROOTS: tuple[Path, ...] = (
    Path("/usr/bin"),
    Path("/usr/local/bin"),
    Path("/bin"),
)
_DEFAULT_HOMEBREW_PREFIX: Path | None = Path("/home/linuxbrew/.linuxbrew")
_EXECUTABLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_MAX_PROBE_BYTES: Final = 1_048_576
_MAX_ARGUMENTS: Final = 128
_MAX_ARGUMENT_BYTES: Final = 16_384
_TERMINATION_GRACE_SECONDS: Final = 0.2
_REAP_GRACE_SECONDS: Final = 0.5
_MAX_DEADLINE_HORIZON_SECONDS: Final = 86_400.0
_RESOLVER_OUTPUT_BYTES: Final = 65_536
# Fixed roots are ownership/mode validated before private ``mkdtemp`` use.
_PROBE_SANDBOX_ROOTS: tuple[Path, ...] = (
    Path("/tmp"),  # nosec B108
    Path("/var/tmp"),  # nosec B108
)
_PROBE_SANDBOX_PREFIX: Final = ".local-agent-probe-"
_ZERO_WRITE_WORKING_DIRECTORY: Final = Path("/")
_ZERO_WRITE_STATE_SINK: Final = Path("/dev/null")
_ZERO_WRITE_CONFIG_DIRECTORY: Final = Path("/proc")
_RESOLVER_HELPER_CODE = """
import json
import socket
import sys

try:
    answers = socket.getaddrinfo(
        sys.argv[1], int(sys.argv[2]), socket.AF_UNSPEC, socket.SOCK_STREAM
    )
    encoded = [
        [family, socktype, protocol, list(sockaddr)]
        for family, socktype, protocol, _canonical, sockaddr in answers
    ]
    sys.stdout.write(json.dumps(encoded, separators=(",", ":")))
except BaseException:
    raise SystemExit(2)
""".strip()
_RUNTIME_ENVIRONMENT_TOOLS = frozenset({"podman", "systemctl", "systemd-run"})
_STATE_ISOLATED_TOOLS = frozenset(
    {"gh", "npm", "ollama", "podman", "systemctl", "systemd-run", "uv"}
)
_SANDBOX_STATE_DIRECTORIES = MappingProxyType(
    {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "state",
    }
)
_GIT_ENVIRONMENT = MappingProxyType(
    {
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
)
_GIT_CONFIGURATION_ARGUMENTS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "diff.external=",
    "-c",
    "diff.trustExitCode=false",
)
_GIT_STATUS_OPTIONS = frozenset(
    {
        "--porcelain",
        "--porcelain=v1",
        "--porcelain=v2",
        "--short",
        "--branch",
        "--show-stash",
        "--untracked-files=no",
        "--untracked-files=normal",
        "--untracked-files=all",
        "--ignored=no",
        "--ignored=traditional",
        "--ignored=matching",
        "--no-renames",
    }
)
_GIT_REV_PARSE_OPTIONS = frozenset(
    {
        "--is-inside-work-tree",
        "--is-bare-repository",
        "--show-toplevel",
        "--show-superproject-working-tree",
        "--show-cdup",
        "--show-prefix",
        "--git-dir",
    }
)


class ProbeError(Exception):
    """A stable probe-boundary failure that never embeds untrusted values."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    duration_ms: int


@dataclass(frozen=True)
class LoopbackResponse:
    status_code: int
    data: object
    body_bytes: int


@dataclass(frozen=True)
class _ApprovedRoot:
    search_root: Path
    containment_root: Path
    validation_start: Path
    allowed_owners: frozenset[int]


@dataclass(frozen=True)
class _ExecutableEvidence:
    identity: str
    canonical_target: Path
    alias_fingerprint: tuple[int, int, int]
    target_fingerprint: tuple[int, int]
    issuer: object


@dataclass(frozen=True)
class _ValidatedExecutable:
    lexical_path: Path
    canonical_target: Path
    identity: str


@dataclass(frozen=True)
class _ProbeSandbox:
    root: Path
    parent: Path
    root_fingerprint: tuple[int, int]
    parent_fingerprint: tuple[int, int]


_TRUSTED_PATH_ISSUER = object()
_ConcretePath = type(Path())


class _TrustedExecutablePath(_ConcretePath):  # type: ignore[misc,valid-type]
    """A lexical executable alias carrying unforgeable in-process evidence."""

    __slots__ = ("_probe_evidence",)

    def __new__(cls, path: Path, *, evidence: _ExecutableEvidence) -> Self:
        return cast(Self, super().__new__(cls, path))

    def __init__(self, path: Path, *, evidence: _ExecutableEvidence) -> None:
        super().__init__(path)
        self._probe_evidence = evidence

    def with_segments(self, *pathsegments: str | os.PathLike[str]) -> Path:
        return Path(*pathsegments)


class TrustedExecutableResolver:
    """Resolve command names only below fixed, ownership-validated roots."""

    def resolve(self, name: str) -> Path:
        failure = ("probe_failed", "executable resolution failed")
        try:
            return self._resolve(name)
        except ProbeError as error:
            failure = (error.code, error.message)
        except Exception:  # noqa: BLE001 - redacted public trust boundary
            failure = ("probe_failed", "executable resolution failed")
        raise ProbeError(*failure)

    def _resolve(self, name: str) -> Path:
        if (
            type(name) is not str
            or not _EXECUTABLE_NAME.fullmatch(name)
            or len(name) > 128
        ):
            raise ProbeError("invalid_executable_name", "executable name is invalid")

        roots = _approved_executable_roots()
        for approved in roots:
            candidate = approved.search_root / name
            try:
                alias_metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ProbeError(
                    "untrusted_executable", "executable metadata is unavailable"
                ) from None
            if alias_metadata.st_uid not in approved.allowed_owners:
                raise ProbeError(
                    "untrusted_executable", "executable alias ownership is unsafe"
                )
            resolved = _resolve_candidate(candidate, approved)
            target_metadata = _validate_executable(resolved, approved)
            evidence = _ExecutableEvidence(
                identity=name,
                canonical_target=resolved,
                alias_fingerprint=(
                    alias_metadata.st_dev,
                    alias_metadata.st_ino,
                    alias_metadata.st_mode,
                ),
                target_fingerprint=(target_metadata.st_dev, target_metadata.st_ino),
                issuer=_TRUSTED_PATH_ISSUER,
            )
            return _TrustedExecutablePath(candidate, evidence=evidence)

        raise ProbeError("executable_not_found", "trusted executable is unavailable")


def build_probe_environment(tool: str, source: Mapping[str, str]) -> Mapping[str, str]:
    """Construct a new per-tool environment; never mutate or inherit ``source``."""
    failure = ("unsafe_environment", "environment is unreadable")
    try:
        return _build_probe_environment(tool, source)
    except ProbeError as error:
        failure = (error.code, error.message)
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        failure = ("unsafe_environment", "environment is unreadable")
    raise ProbeError(*failure)


def _build_probe_environment(tool: str, source: Mapping[str, str]) -> Mapping[str, str]:
    if type(tool) is not str or not _EXECUTABLE_NAME.fullmatch(tool):
        raise ProbeError("invalid_tool", "probe tool name is invalid")
    try:
        source_copy = dict(source)
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        raise ProbeError("unsafe_environment", "environment is unreadable") from None
    if any(
        type(key) is not str or type(value) is not str
        for key, value in source_copy.items()
    ):
        raise ProbeError("unsafe_environment", "environment contains invalid values")

    environment: dict[str, str] = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    normalized_tool = Path(tool).name
    if normalized_tool == "git":
        environment.update(_GIT_ENVIRONMENT)
    if normalized_tool in _RUNTIME_ENVIRONMENT_TOOLS:
        runtime_value = source_copy.get("XDG_RUNTIME_DIR")
        if runtime_value is not None:
            runtime_path = _validated_runtime_directory(runtime_value)
            environment["XDG_RUNTIME_DIR"] = str(runtime_path)
        dbus_value = source_copy.get("DBUS_SESSION_BUS_ADDRESS")
        if dbus_value is not None:
            environment["DBUS_SESSION_BUS_ADDRESS"] = _validated_dbus_address(
                dbus_value, runtime_value
            )
    return MappingProxyType(environment)


def run_bounded_process(
    executable: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
    deadline_monotonic: float,
    max_bytes: int,
) -> ProcessResult:
    """Run a trusted executable with a hard deadline and combined output cap."""
    failure = ("probe_failed", "probe process failed")
    try:
        return _run_bounded_process(
            executable,
            arguments,
            environment,
            deadline_monotonic,
            max_bytes,
        )
    except ProbeError as error:
        failure = (error.code, error.message)
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        failure = ("probe_failed", "probe process failed")
    raise ProbeError(*failure)


def run_zero_write_process(
    executable: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
    deadline_monotonic: float,
    max_bytes: int,
) -> ProcessResult:
    """Run a trusted Doctor command without creating filesystem objects."""
    failure = ("probe_failed", "probe process failed")
    try:
        return _run_zero_write_process(
            executable,
            arguments,
            environment,
            deadline_monotonic,
            max_bytes,
        )
    except ProbeError as error:
        failure = (error.code, error.message)
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        failure = ("probe_failed", "probe process failed")
    raise ProbeError(*failure)


def _run_zero_write_process(
    executable: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
    deadline_monotonic: float,
    max_bytes: int,
) -> ProcessResult:
    _validate_max_bytes(max_bytes)
    deadline = _validated_deadline(deadline_monotonic)
    if time.monotonic() >= deadline:
        raise ProbeError("deadline_expired", "probe deadline has expired")
    trusted_executable = _validated_explicit_executable(executable)
    safe_arguments = _validated_arguments(arguments)
    safe_environment = _build_probe_environment(
        trusted_executable.identity, environment
    )
    if trusted_executable.identity == "git":
        safe_arguments = _harden_git_arguments(safe_arguments)
    safe_environment = _zero_write_probe_environment(
        trusted_executable.identity, safe_environment
    )
    working_directory = _validated_zero_write_working_directory()
    if time.monotonic() >= deadline:
        raise ProbeError("deadline_expired", "probe deadline has expired")

    started = time.monotonic()
    try:
        process = subprocess.Popen(  # nosec B603
            (str(trusted_executable.canonical_target), *safe_arguments),
            shell=False,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(safe_environment),
            cwd=working_directory,
            close_fds=True,
            bufsize=0,
        )
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        raise ProbeError(
            "process_start_failed", "probe process could not start"
        ) from None

    try:
        stdout, stderr, timed_out, truncated = _capture_process(
            process, deadline, max_bytes
        )
        duration_ms = max(0, int((time.monotonic() - started) * 1_000))
        returncode = process.returncode
        if returncode is None:
            _terminate_process_group(process)
            returncode = process.returncode
        if returncode is None:
            raise ProbeError("process_reap_failed", "probe process was not reaped")
        return ProcessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=truncated,
            duration_ms=duration_ms,
        )
    except ProbeError:
        raise
    except Exception:  # noqa: BLE001 - redacted process boundary
        raise ProbeError("probe_failed", "probe process failed") from None


def _validated_zero_write_working_directory() -> Path:
    path = _ZERO_WRITE_WORKING_DIRECTORY
    try:
        metadata = path.lstat()
    except OSError:
        raise ProbeError(
            "zero_write_context_failed", "Doctor process context is unsafe"
        ) from None
    if (
        type(path) is not _ConcretePath
        or path != Path("/")
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise ProbeError(
            "zero_write_context_failed", "Doctor process context is unsafe"
        )
    return path


def _zero_write_probe_environment(
    tool: str, environment: Mapping[str, str]
) -> Mapping[str, str]:
    isolated = dict(environment)
    if tool not in _STATE_ISOLATED_TOOLS:
        return MappingProxyType(isolated)
    sink = _validated_zero_write_state_sink()
    for key in _SANDBOX_STATE_DIRECTORIES:
        isolated[key] = str(sink)
    if tool == "gh":
        isolated["GH_CONFIG_DIR"] = str(_validated_zero_write_config_directory())
    return MappingProxyType(isolated)


def _validated_zero_write_state_sink() -> Path:
    path = _ZERO_WRITE_STATE_SINK
    try:
        metadata = path.lstat()
    except OSError:
        raise ProbeError(
            "zero_write_context_failed", "Doctor process state sink is unsafe"
        ) from None
    if (
        type(path) is not _ConcretePath
        or path != Path("/dev/null")
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISCHR(metadata.st_mode)
        or metadata.st_uid != 0
    ):
        raise ProbeError(
            "zero_write_context_failed", "Doctor process state sink is unsafe"
        )
    return path


def _validated_zero_write_config_directory() -> Path:
    path = _ZERO_WRITE_CONFIG_DIRECTORY
    try:
        metadata = path.lstat()
    except OSError:
        raise ProbeError(
            "zero_write_context_failed", "Doctor process config sink is unsafe"
        ) from None
    if (
        type(path) is not _ConcretePath
        or path != Path("/proc")
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise ProbeError(
            "zero_write_context_failed", "Doctor process config sink is unsafe"
        )
    return path


def _run_bounded_process(
    executable: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str],
    deadline_monotonic: float,
    max_bytes: int,
) -> ProcessResult:
    _validate_max_bytes(max_bytes)
    deadline = _validated_deadline(deadline_monotonic)
    if time.monotonic() >= deadline:
        raise ProbeError("deadline_expired", "probe deadline has expired")
    trusted_executable = _validated_explicit_executable(executable)
    safe_arguments = _validated_arguments(arguments)
    safe_environment = _build_probe_environment(
        trusted_executable.identity, environment
    )
    if trusted_executable.identity == "git":
        safe_arguments = _harden_git_arguments(safe_arguments)
    if time.monotonic() >= deadline:
        raise ProbeError("deadline_expired", "probe deadline has expired")

    started = time.monotonic()
    sandbox = _create_probe_sandbox()
    process: subprocess.Popen[bytes] | None = None
    result: ProcessResult | None = None
    primary_failure: ProbeError | None = None
    try:
        safe_environment = _sandbox_probe_environment(
            trusted_executable.identity, safe_environment, sandbox
        )
        if time.monotonic() >= deadline:
            raise ProbeError("deadline_expired", "probe deadline has expired")
        process = subprocess.Popen(  # nosec B603
            (str(trusted_executable.canonical_target), *safe_arguments),
            shell=False,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(safe_environment),
            cwd=sandbox.root,
            close_fds=True,
            bufsize=0,
        )
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        primary_failure = ProbeError(
            "process_start_failed", "probe process could not start"
        )
    except ProbeError as error:
        primary_failure = error
    else:
        try:
            stdout, stderr, timed_out, truncated = _capture_process(
                process, deadline, max_bytes
            )

            duration_ms = max(0, int((time.monotonic() - started) * 1_000))
            returncode = process.returncode
            if returncode is None:
                _terminate_process_group(process)
                returncode = process.returncode
            if returncode is None:
                raise ProbeError("process_reap_failed", "probe process was not reaped")
            result = ProcessResult(
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                truncated=truncated,
                duration_ms=duration_ms,
            )
        except ProbeError as error:
            primary_failure = error
        except Exception:  # noqa: BLE001 - redacted process boundary
            primary_failure = ProbeError("probe_failed", "probe process failed")
    finally:
        cleanup_failure = _cleanup_probe_sandbox(sandbox)
        if primary_failure is None and cleanup_failure is not None:
            primary_failure = cleanup_failure

    if primary_failure is not None:
        raise primary_failure
    if result is None:
        raise ProbeError("probe_failed", "probe process failed")
    return result


def _create_probe_sandbox() -> _ProbeSandbox:
    cleanup_failed = False
    for parent in _PROBE_SANDBOX_ROOTS:
        candidate: Path | None = None
        parent_fingerprint: tuple[int, int] | None = None
        try:
            parent_path, parent_metadata = _validated_probe_sandbox_parent(parent)
            parent_fingerprint = (parent_metadata.st_dev, parent_metadata.st_ino)
            raw_root = tempfile.mkdtemp(prefix=_PROBE_SANDBOX_PREFIX, dir=parent_path)
            root = Path(raw_root)
            candidate = root
            root_metadata = root.lstat()
            current_parent = parent_path.lstat()
            if (
                root.parent != parent_path
                or not root.name.startswith(_PROBE_SANDBOX_PREFIX)
                or stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid != os.getuid()
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
                or (current_parent.st_dev, current_parent.st_ino)
                != (parent_metadata.st_dev, parent_metadata.st_ino)
            ):
                raise OSError(errno.EPERM, "unsafe probe sandbox")
            return _ProbeSandbox(
                root=root,
                parent=parent_path,
                root_fingerprint=(root_metadata.st_dev, root_metadata.st_ino),
                parent_fingerprint=(parent_metadata.st_dev, parent_metadata.st_ino),
            )
        except Exception:  # noqa: BLE001 - redacted sandbox creation boundary
            if candidate is not None and not _discard_probe_sandbox_candidate(
                candidate, parent, parent_fingerprint
            ):
                cleanup_failed = True
                break
            continue
    if cleanup_failed:
        raise ProbeError("sandbox_cleanup_failed", "probe sandbox could not be removed")
    raise ProbeError("sandbox_create_failed", "probe sandbox could not be created")


def _discard_probe_sandbox_candidate(
    candidate: Path,
    parent: Path,
    parent_fingerprint: tuple[int, int] | None,
) -> bool:
    try:
        parent_metadata = parent.lstat()
        metadata = candidate.lstat()
        if (
            parent_fingerprint is None
            or stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or (parent_metadata.st_dev, parent_metadata.st_ino) != parent_fingerprint
            or candidate.parent != parent
            or not candidate.name.startswith(_PROBE_SANDBOX_PREFIX)
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            return False
        shutil.rmtree(candidate)
        return True
    except Exception:  # noqa: BLE001 - best-effort failed-creation cleanup
        return False


def _validated_probe_sandbox_parent(parent: Path) -> tuple[Path, os.stat_result]:
    if (
        type(parent) is not _ConcretePath
        or not parent.is_absolute()
        or Path(os.path.normpath(os.fspath(parent))) != parent
    ):
        raise OSError(errno.EINVAL, "unsafe probe sandbox parent")
    _validate_safe_directory_ancestors(parent, "sandbox_create_failed")
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(errno.EPERM, "unsafe probe sandbox parent")
    if metadata.st_uid not in {0, os.getuid()}:
        raise OSError(errno.EPERM, "unsafe probe sandbox parent")
    writable = bool(metadata.st_mode & 0o022)
    sticky_root = bool(metadata.st_mode & stat.S_ISVTX) and metadata.st_uid == 0
    if writable and not sticky_root:
        raise OSError(errno.EPERM, "unsafe probe sandbox parent")
    return parent, metadata


def _sandbox_probe_environment(
    tool: str,
    environment: Mapping[str, str],
    sandbox: _ProbeSandbox,
) -> Mapping[str, str]:
    isolated = dict(environment)
    if tool not in _STATE_ISOLATED_TOOLS:
        return MappingProxyType(isolated)
    try:
        for key, relative in _SANDBOX_STATE_DIRECTORIES.items():
            path = sandbox.root / relative
            path.mkdir(mode=0o700)
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or path.parent != sandbox.root
            ):
                raise OSError(errno.EPERM, "unsafe probe state directory")
            isolated[key] = str(path)
    except Exception:  # noqa: BLE001 - redacted sandbox preparation boundary
        raise ProbeError(
            "sandbox_prepare_failed", "probe sandbox could not be prepared"
        ) from None
    return MappingProxyType(isolated)


def _cleanup_probe_sandbox(sandbox: _ProbeSandbox) -> ProbeError | None:
    try:
        parent_metadata = sandbox.parent.lstat()
        root_metadata = sandbox.root.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or (parent_metadata.st_dev, parent_metadata.st_ino)
            != sandbox.parent_fingerprint
            or sandbox.root.parent != sandbox.parent
            or stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or (root_metadata.st_dev, root_metadata.st_ino) != sandbox.root_fingerprint
        ):
            raise OSError(errno.EPERM, "unsafe probe sandbox cleanup")
        shutil.rmtree(sandbox.root)
    except Exception:  # noqa: BLE001 - redacted sandbox cleanup boundary
        return ProbeError(
            "sandbox_cleanup_failed", "probe sandbox could not be removed"
        )
    return None


def get_loopback_json(
    endpoint: str | Path,
    request_path: str,
    deadline_monotonic: float,
    max_bytes: int,
) -> LoopbackResponse:
    """Fetch bounded JSON from a pinned loopback address or trusted Unix socket."""
    failure = ("loopback_failed", "loopback probe failed")
    try:
        return _get_loopback_json(
            endpoint,
            request_path,
            deadline_monotonic,
            max_bytes,
            zero_write=False,
        )
    except ProbeError as error:
        failure = (error.code, error.message)
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        failure = ("loopback_failed", "loopback probe failed")
    raise ProbeError(*failure)


def get_loopback_json_zero_write(
    endpoint: str | Path,
    request_path: str,
    deadline_monotonic: float,
    max_bytes: int,
) -> LoopbackResponse:
    """Fetch bounded loopback JSON with a zero-write resolver helper."""
    failure = ("loopback_failed", "loopback probe failed")
    try:
        return _get_loopback_json(
            endpoint,
            request_path,
            deadline_monotonic,
            max_bytes,
            zero_write=True,
        )
    except ProbeError as error:
        failure = (error.code, error.message)
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        failure = ("loopback_failed", "loopback probe failed")
    raise ProbeError(*failure)


def _get_loopback_json(
    endpoint: str | Path,
    request_path: str,
    deadline_monotonic: float,
    max_bytes: int,
    *,
    zero_write: bool,
) -> LoopbackResponse:
    _validate_max_bytes(max_bytes)
    deadline = _validated_deadline(deadline_monotonic)
    if time.monotonic() >= deadline:
        raise ProbeError("deadline_expired", "probe deadline has expired")
    safe_request_path = _validated_request_path(request_path)

    connection: http.client.HTTPConnection | None = None
    raw_socket: socket.socket | None = None
    deadline_guard: _SocketDeadlineGuard | None = None
    try:
        if isinstance(endpoint, Path):
            socket_path = _validated_unix_socket(endpoint)
            raw_socket = _connect_unix_socket(socket_path, deadline)
            connection = http.client.HTTPConnection(
                "localhost", timeout=_remaining(deadline)
            )
        elif type(endpoint) is str:
            host, port = _parse_http_endpoint(endpoint)
            addresses = _resolve_loopback_addresses(
                host, port, deadline, zero_write=zero_write
            )
            raw_socket = _connect_loopback(addresses, deadline)
            connection = http.client.HTTPConnection(
                host, port=port, timeout=_remaining(deadline)
            )
        else:
            raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")

        deadline_guard = _SocketDeadlineGuard(raw_socket, deadline)
        bounded_socket = _HeaderBoundedSocket(raw_socket, max_bytes)
        connection.sock = cast(socket.socket, bounded_socket)
        connection.putrequest(
            "GET",
            safe_request_path,
            skip_accept_encoding=True,
        )
        connection.putheader("Accept", "application/json")
        connection.putheader("Connection", "close")
        connection.endheaders()
        _set_connection_timeout(raw_socket, deadline)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ProbeError("redirect_forbidden", "loopback redirects are forbidden")
        body = _read_response_body(response, raw_socket, deadline, max_bytes)
        if deadline_guard.expired:
            raise ProbeError("probe_timeout", "loopback probe timed out")
        try:
            data = json.loads(
                body.decode("utf-8"),
                parse_constant=lambda _value: _reject_json_constant(),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ProbeError(
                "invalid_json", "loopback response is not valid JSON"
            ) from None
        _remaining(deadline)
        if deadline_guard.expired:
            raise ProbeError("probe_timeout", "loopback probe timed out")
        _remaining(deadline)
        return LoopbackResponse(
            status_code=response.status,
            data=data,
            body_bytes=len(body),
        )
    except ProbeError:
        raise
    except _HeaderLimitExceeded:
        raise ProbeError(
            "response_too_large", "loopback response headers exceed byte limit"
        ) from None
    except TimeoutError:
        raise ProbeError("probe_timeout", "loopback probe timed out") from None
    except Exception:  # noqa: BLE001 - redacted public trust boundary
        if deadline_guard is not None and deadline_guard.expired:
            raise ProbeError("probe_timeout", "loopback probe timed out") from None
        raise ProbeError("loopback_failed", "loopback probe failed") from None
    finally:
        cleanup_failed = False
        if deadline_guard is not None:
            try:
                deadline_guard.close()
            except Exception:  # noqa: BLE001 - hostile socket cleanup boundary
                cleanup_failed = True
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - hostile socket cleanup boundary
                cleanup_failed = True
        elif raw_socket is not None:
            try:
                raw_socket.close()
            except Exception:  # noqa: BLE001 - hostile socket cleanup boundary
                cleanup_failed = True
        if cleanup_failed:
            raise ProbeError("loopback_cleanup_failed", "loopback probe cleanup failed")


def _approved_executable_roots() -> tuple[_ApprovedRoot, ...]:
    approved: list[_ApprovedRoot] = []
    seen: set[Path] = set()
    for configured in _SYSTEM_EXECUTABLE_ROOTS:
        try:
            canonical = configured.resolve(strict=True)
        except FileNotFoundError:
            continue
        except OSError:
            raise ProbeError(
                "untrusted_executable_root", "system executable root is unsafe"
            ) from None
        if canonical in seen:
            continue
        root = _ApprovedRoot(
            canonical,
            canonical,
            Path("/"),
            frozenset({0, os.getuid()}),
        )
        try:
            _validate_directory_chain(
                root.validation_start,
                canonical,
                root.allowed_owners,
                "untrusted_executable_root",
            )
        except ProbeError:
            # A host may expose one optional executable root with unsafe
            # ownership/mode.  Ignore that root and continue with the fixed
            # roots that pass validation; never relax its checks.
            continue
        seen.add(canonical)
        approved.append(root)

    prefix = _DEFAULT_HOMEBREW_PREFIX
    if prefix is not None and prefix.exists():
        try:
            canonical_prefix = _validated_homebrew_prefix(prefix)
        except ProbeError:
            # Homebrew is optional.  If the fixed system roots already
            # provide a trusted execution source, ignore an unsafe optional
            # Homebrew tree; retain the strict failure when it is the only
            # configured source.
            if approved:
                return tuple(approved)
            raise
        bin_root = canonical_prefix / "bin"
        try:
            canonical_bin = bin_root.resolve(strict=True)
        except FileNotFoundError:
            canonical_bin = None
        except OSError:
            raise ProbeError(
                "untrusted_executable_root", "Homebrew executable root is unsafe"
            ) from None
        if canonical_bin is not None and canonical_bin not in seen:
            owners = frozenset({0, os.getuid()})
            root = _ApprovedRoot(
                canonical_bin,
                canonical_prefix,
                canonical_prefix,
                owners,
            )
            _validate_directory_chain(
                canonical_prefix,
                canonical_bin,
                owners,
                "untrusted_executable_root",
            )
            seen.add(canonical_bin)
            approved.append(root)
    return tuple(approved)


def _validated_homebrew_prefix(prefix: Path) -> Path:
    if not prefix.is_absolute() or Path(os.path.normpath(os.fspath(prefix))) != prefix:
        raise ProbeError(
            "untrusted_executable_root", "Homebrew prefix is not normalized"
        )
    try:
        metadata = prefix.lstat()
        canonical = prefix.resolve(strict=True)
    except OSError:
        raise ProbeError(
            "untrusted_executable_root", "Homebrew prefix is unavailable"
        ) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ProbeError("untrusted_executable_root", "Homebrew prefix is unsafe")
    _validate_owned_mode(
        metadata,
        frozenset({0, os.getuid()}),
        "untrusted_executable_root",
    )
    _validate_directory_chain(
        Path("/"),
        canonical,
        frozenset({0, os.getuid()}),
        "untrusted_executable_root",
    )
    return canonical


def _resolve_candidate(candidate: Path, approved: _ApprovedRoot) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(approved.containment_root)
    except (OSError, ValueError):
        raise ProbeError(
            "untrusted_executable", "executable escapes its approved root"
        ) from None
    return resolved


def _validate_executable(path: Path, approved: _ApprovedRoot) -> os.stat_result:
    _validate_directory_chain(
        approved.validation_start,
        path.parent,
        approved.allowed_owners,
        "untrusted_executable",
    )
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProbeError(
            "untrusted_executable", "executable cannot be opened safely"
        ) from None
    descriptor_metadata: os.stat_result | None = None
    path_metadata: os.stat_result | None = None
    metadata_failed = False
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = path.stat(follow_symlinks=False)
    except OSError:
        metadata_failed = True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            raise ProbeError(
                "untrusted_executable", "executable descriptor could not be closed"
            ) from None
    if metadata_failed or descriptor_metadata is None or path_metadata is None:
        raise ProbeError("untrusted_executable", "executable metadata is unavailable")
    if (
        not stat.S_ISREG(descriptor_metadata.st_mode)
        or (descriptor_metadata.st_mode & 0o111) == 0
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise ProbeError("untrusted_executable", "executable is not a trusted file")
    _validate_owned_mode(
        descriptor_metadata, approved.allowed_owners, "untrusted_executable"
    )
    if not os.access(path, os.X_OK):
        raise ProbeError("untrusted_executable", "executable is not runnable")
    return descriptor_metadata


def _validate_directory_chain(
    start: Path,
    end: Path,
    allowed_owners: frozenset[int],
    code: str,
) -> None:
    try:
        relative = end.relative_to(start)
    except ValueError:
        raise ProbeError(code, "approved executable root is inconsistent") from None
    current = start
    components = (Path("."), *relative.parents[::-1], relative)
    checked: set[Path] = set()
    for component in components:
        path = current if component == Path(".") else start / component
        if path in checked:
            continue
        checked.add(path)
        try:
            metadata = path.lstat()
        except OSError:
            raise ProbeError(code, "executable ancestor is unavailable") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProbeError(code, "executable ancestor is unsafe")
        _validate_owned_mode(metadata, allowed_owners, code)


def _validate_owned_mode(
    metadata: os.stat_result, allowed_owners: frozenset[int], code: str
) -> None:
    if metadata.st_uid not in allowed_owners or metadata.st_mode & 0o022:
        raise ProbeError(code, "trusted path ownership or mode is unsafe")


def _validated_explicit_executable(executable: Path) -> _ValidatedExecutable:
    if type(executable) is not _TrustedExecutablePath or not executable.is_absolute():
        raise ProbeError("untrusted_executable", "executable path is invalid")
    evidence = executable._probe_evidence
    if evidence.issuer is not _TRUSTED_PATH_ISSUER:
        raise ProbeError("untrusted_executable", "executable evidence is invalid")
    try:
        alias_metadata = executable.lstat()
        resolved = executable.resolve(strict=True)
    except OSError:
        raise ProbeError(
            "untrusted_executable", "executable path is unavailable"
        ) from None
    if (
        resolved != evidence.canonical_target
        or executable.name != evidence.identity
        or (
            alias_metadata.st_dev,
            alias_metadata.st_ino,
            alias_metadata.st_mode,
        )
        != evidence.alias_fingerprint
    ):
        raise ProbeError("untrusted_executable", "executable evidence changed")
    for approved in _approved_executable_roots():
        try:
            executable.relative_to(approved.search_root)
            resolved.relative_to(approved.containment_root)
        except ValueError:
            continue
        target_metadata = _validate_executable(resolved, approved)
        if (target_metadata.st_dev, target_metadata.st_ino) != (
            evidence.target_fingerprint
        ):
            raise ProbeError("untrusted_executable", "executable target changed")
        return _ValidatedExecutable(executable, resolved, evidence.identity)
    raise ProbeError(
        "untrusted_executable", "executable path is outside approved roots"
    )


def _validated_runtime_directory(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or Path(os.path.normpath(raw)) != path or "\x00" in raw:
        raise ProbeError("unsafe_environment", "runtime directory is unsafe")
    try:
        _validate_safe_directory_ancestors(path, "unsafe_environment")
        metadata = path.lstat()
    except ProbeError:
        raise
    except OSError:
        raise ProbeError(
            "unsafe_environment", "runtime directory is unavailable"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ProbeError("unsafe_environment", "runtime directory is unsafe")
    return path


def _validate_safe_directory_ancestors(path: Path, code: str) -> None:
    current = Path("/")
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            raise ProbeError(
                code, "trusted directory ancestor is unavailable"
            ) from None
        writable = bool(metadata.st_mode & 0o022)
        sticky_root = bool(metadata.st_mode & stat.S_ISVTX) and metadata.st_uid == 0
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or (writable and not sticky_root)
        ):
            raise ProbeError(code, "trusted directory ancestor is unsafe")


def _validated_dbus_address(raw: str, runtime_value: str | None) -> str:
    prefix = "unix:path="
    if runtime_value is None or not raw.startswith(prefix) or "," in raw:
        raise ProbeError("unsafe_environment", "session bus address is unsafe")
    socket_path = Path(raw.removeprefix(prefix))
    runtime = _validated_runtime_directory(runtime_value)
    if (
        not socket_path.is_absolute()
        or Path(os.path.normpath(os.fspath(socket_path))) != socket_path
    ):
        raise ProbeError("unsafe_environment", "session bus address is unsafe")
    try:
        socket_path.relative_to(runtime)
        _validated_unix_socket(socket_path)
    except (OSError, ValueError, ProbeError):
        raise ProbeError(
            "unsafe_environment", "session bus address is unsafe"
        ) from None
    return raw


def _validated_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if type(arguments) is not tuple or len(arguments) > _MAX_ARGUMENTS:
        raise ProbeError("invalid_arguments", "probe arguments are invalid")
    total = 0
    for argument in arguments:
        if type(argument) is not str or "\x00" in argument:
            raise ProbeError("invalid_arguments", "probe arguments are invalid")
        try:
            total += len(argument.encode("utf-8"))
        except UnicodeEncodeError:
            raise ProbeError(
                "invalid_arguments", "probe arguments are invalid"
            ) from None
        if total > _MAX_ARGUMENT_BYTES:
            raise ProbeError("invalid_arguments", "probe arguments are too large")
    return arguments


def _harden_git_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
    if arguments == ("--version",) or arguments == ("version",):
        return (*_GIT_CONFIGURATION_ARGUMENTS, *arguments)
    index = 0
    prefix: list[str] = []
    while index < len(arguments) and arguments[index] == "-C":
        if index + 1 >= len(arguments):
            raise ProbeError("unsafe_git_arguments", "Git arguments are unsafe")
        directory = arguments[index + 1]
        if not directory or "\x00" in directory:
            raise ProbeError("unsafe_git_arguments", "Git arguments are unsafe")
        prefix.extend(("-C", directory))
        index += 2
    if index >= len(arguments):
        raise ProbeError("unsafe_git_arguments", "Git subcommand is missing")
    subcommand = arguments[index]
    options = arguments[index + 1 :]
    if subcommand == "status":
        allowed = _GIT_STATUS_OPTIONS
    elif subcommand == "rev-parse":
        allowed = _GIT_REV_PARSE_OPTIONS
    else:
        raise ProbeError("unsafe_git_arguments", "Git subcommand is not approved")
    if not options or any(option not in allowed for option in options):
        raise ProbeError("unsafe_git_arguments", "Git options are not approved")
    return (
        *_GIT_CONFIGURATION_ARGUMENTS,
        *prefix,
        subcommand,
        *options,
    )


def _capture_process(
    process: subprocess.Popen[bytes], deadline: float, max_bytes: int
) -> tuple[bytes, bytes, bool, bool]:
    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        raise ProbeError("process_io_failed", "probe process pipes are unavailable")
    selector: selectors.BaseSelector | None = None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    timed_out = False
    truncated = False
    failure: ProbeError | None = None
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
                break
            events = selector.select(remaining)
            if not events:
                timed_out = True
                _terminate_process_group(process)
                break
            for key, _mask in events:
                stream = cast(Any, key.fileobj)
                allowance = max_bytes - total
                chunk = os.read(stream.fileno(), min(65_536, allowance + 1))
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                destination = cast(str, key.data)
                if len(chunk) > allowance:
                    output[destination].extend(chunk[:allowance])
                    total += allowance
                    truncated = True
                    _terminate_process_group(process)
                    break
                output[destination].extend(chunk)
                total += len(chunk)
            if truncated:
                break
        if not timed_out and not truncated:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_process_group(process)
    except ProbeError as error:
        failure = error
    except Exception:  # noqa: BLE001 - hostile selector/pipe boundary
        failure = ProbeError("process_io_failed", "probe process I/O failed")
    finally:
        if failure is not None and process.returncode is None:
            try:
                _terminate_process_group(process)
            except ProbeError as termination_error:
                failure = termination_error
        cleanup_failed = False
        if selector is not None:
            try:
                selector.close()
            except Exception:  # noqa: BLE001 - hostile selector boundary
                cleanup_failed = True
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - hostile pipe boundary
                cleanup_failed = True
        if failure is None and cleanup_failed:
            failure = ProbeError(
                "process_cleanup_failed", "probe process cleanup failed"
            )
    if failure is not None:
        raise failure
    return bytes(output["stdout"]), bytes(output["stderr"]), timed_out, truncated


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    group_id = process.pid
    failed = False
    try:
        os.killpg(group_id, signal.SIGTERM)
    except OSError as error:
        if error.errno != errno.ESRCH:
            failed = True
    grace_deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    members, scan_failed = _live_process_group_members(group_id)
    failed = failed or scan_failed
    while time.monotonic() < grace_deadline and members:
        time.sleep(0.005)
        members, scan_failed = _live_process_group_members(group_id)
        failed = failed or scan_failed
    if members:
        try:
            os.killpg(group_id, signal.SIGKILL)
        except OSError as error:
            if error.errno != errno.ESRCH:
                failed = True
        kill_deadline = time.monotonic() + _REAP_GRACE_SECONDS
        while time.monotonic() < kill_deadline and members:
            time.sleep(0.005)
            members, scan_failed = _live_process_group_members(group_id)
            failed = failed or scan_failed
    for _attempt in range(2):
        if process.returncode is not None:
            break
        try:
            process.wait(timeout=_REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            failed = True
        except OSError:
            failed = True
    members, scan_failed = _live_process_group_members(group_id)
    if failed or scan_failed or members or process.returncode is None:
        raise ProbeError(
            "termination_failed", "probe process containment could not be confirmed"
        )


def _live_process_group_members(group_id: int) -> tuple[tuple[int, ...], bool]:
    proc_root = Path("/proc")
    try:
        proc_metadata = proc_root.stat()
    except FileNotFoundError:
        return (() if not _process_group_exists(group_id) else (group_id,)), False
    except OSError:
        return (), True
    if not stat.S_ISDIR(proc_metadata.st_mode):
        return (() if not _process_group_exists(group_id) else (group_id,)), False
    members: list[int] = []
    try:
        entries = tuple(proc_root.glob("[0-9]*"))
    except OSError:
        return (), True
    for entry in entries:
        try:
            pid = int(entry.name)
            raw = (entry / "stat").read_text(encoding="utf-8")
            _prefix, separator, suffix = raw.rpartition(")")
            fields = suffix.split()
            if not separator or len(fields) < 3:
                return (), True
            state = fields[0]
            process_group = int(fields[2])
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError):
            return (), True
        if process_group == group_id and state != "Z":
            members.append(pid)
    return tuple(members), False


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except OSError as error:
        return error.errno != errno.ESRCH
    return True


def _validate_max_bytes(max_bytes: int) -> None:
    if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_PROBE_BYTES:
        raise ProbeError("invalid_max_bytes", "probe byte limit is invalid")


def _validated_deadline(deadline: float) -> float:
    if type(deadline) not in (int, float):
        raise ProbeError("invalid_deadline", "probe deadline is invalid")
    try:
        value = float(deadline)
        now = time.monotonic()
        horizon = value - now
    except (OverflowError, OSError, RuntimeError, TypeError, ValueError):
        raise ProbeError("invalid_deadline", "probe deadline is invalid") from None
    if (
        not math.isfinite(value)
        or not math.isfinite(horizon)
        or horizon > _MAX_DEADLINE_HORIZON_SECONDS
    ):
        raise ProbeError("invalid_deadline", "probe deadline is invalid")
    return value


def _validated_request_path(request_path: str) -> str:
    if (
        type(request_path) is not str
        or not request_path.startswith("/")
        or request_path.startswith("//")
        or len(request_path.encode("utf-8", errors="ignore")) > 4_096
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in request_path
        )
        or "#" in request_path
    ):
        raise ProbeError("invalid_request_path", "loopback request path is invalid")
    return request_path


def _parse_http_endpoint(endpoint: str) -> tuple[str, int]:
    if len(endpoint) > 2_048 or any(ord(character) < 0x20 for character in endpoint):
        raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        raise ProbeError("invalid_endpoint", "loopback endpoint is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
    authority = parsed.netloc
    if authority.startswith("["):
        closing = authority.find("]")
        if closing <= 1:
            raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
        port_suffix = authority[closing + 1 :]
        if not port_suffix:
            port = 80
        elif port_suffix.startswith(":"):
            port = _validated_explicit_port(port_suffix[1:])
        else:
            raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
    else:
        colon_count = authority.count(":")
        if colon_count == 0:
            port = 80
        elif colon_count == 1:
            _host_authority, raw_port = authority.rsplit(":", 1)
            port = _validated_explicit_port(raw_port)
        else:
            raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if host.lower().rstrip(".") != "localhost":
            raise ProbeError(
                "non_loopback_endpoint", "endpoint hostname is not approved"
            ) from None
    else:
        if not literal.is_loopback:
            raise ProbeError(
                "non_loopback_endpoint", "endpoint address is not loopback"
            )
    return host, port


def _validated_explicit_port(raw_port: str) -> int:
    if not raw_port or not raw_port.isascii() or not raw_port.isdigit():
        raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
    try:
        port = int(raw_port, 10)
    except (OverflowError, ValueError):
        raise ProbeError("invalid_endpoint", "loopback endpoint is invalid") from None
    if not 1 <= port <= 65_535:
        raise ProbeError("invalid_endpoint", "loopback endpoint is invalid")
    return port


def _resolve_loopback_addresses(
    host: str, port: int, deadline: float, *, zero_write: bool
) -> tuple[tuple[int, int, int, tuple[Any, ...]], ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        python = TrustedExecutableResolver().resolve("python3")
        runner = run_zero_write_process if zero_write else run_bounded_process
        resolver_result = runner(
            python,
            ("-I", "-S", "-c", _RESOLVER_HELPER_CODE, host, str(port)),
            {},
            deadline,
            _RESOLVER_OUTPUT_BYTES,
        )
        if resolver_result.timed_out:
            raise ProbeError("probe_timeout", "loopback name resolution timed out")
        if resolver_result.truncated or resolver_result.returncode != 0:
            raise ProbeError(
                "loopback_resolution_failed", "loopback name resolution failed"
            )
        try:
            raw_answers = json.loads(resolver_result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise ProbeError(
                "loopback_resolution_failed", "loopback name resolution failed"
            ) from None
        _remaining(deadline)
    else:
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        sockaddr: tuple[Any, ...]
        if family == socket.AF_INET6:
            sockaddr = (str(literal), port, 0, 0)
        else:
            sockaddr = (str(literal), port)
        raw_answers = [[family, socket.SOCK_STREAM, socket.IPPROTO_TCP, sockaddr]]
    if not isinstance(raw_answers, list) or not raw_answers:
        raise ProbeError(
            "loopback_resolution_failed", "loopback name resolution failed"
        )

    addresses: list[tuple[int, int, int, tuple[Any, ...]]] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for answer in raw_answers:
        try:
            family, socktype, protocol, sockaddr = answer
            if (
                family not in {socket.AF_INET, socket.AF_INET6}
                or socktype != socket.SOCK_STREAM
                or protocol not in {0, socket.IPPROTO_TCP}
                or not isinstance(sockaddr, (list, tuple))
                or (family == socket.AF_INET and len(sockaddr) != 2)
                or (family == socket.AF_INET6 and len(sockaddr) != 4)
                or type(sockaddr[1]) is not int
                or sockaddr[1] != port
            ):
                raise ValueError
            address = str(sockaddr[0])
            if not ipaddress.ip_address(address).is_loopback:
                raise ProbeError(
                    "non_loopback_endpoint", "resolved endpoint is not loopback"
                )
            key = (int(family), tuple(sockaddr))
            if key not in seen:
                seen.add(key)
                addresses.append(
                    (int(family), int(socktype), int(protocol), tuple(sockaddr))
                )
        except ProbeError:
            raise
        except (IndexError, TypeError, ValueError):
            raise ProbeError(
                "loopback_resolution_failed", "loopback name resolution failed"
            ) from None
    return tuple(addresses)


def _connect_loopback(
    addresses: tuple[tuple[int, int, int, tuple[Any, ...]], ...], deadline: float
) -> socket.socket:
    for family, socktype, protocol, sockaddr in addresses:
        candidate: socket.socket | None = None
        try:
            candidate = socket.socket(family, socktype, protocol)
            candidate.settimeout(_remaining(deadline))
            candidate.connect(sockaddr)
            peer = candidate.getpeername()
            if not ipaddress.ip_address(str(peer[0])).is_loopback:
                raise ProbeError("non_loopback_peer", "connected peer is not loopback")
            return candidate
        except ProbeError:
            if candidate is not None:
                candidate.close()
            raise
        except (OSError, TimeoutError):
            if candidate is not None:
                candidate.close()
            if time.monotonic() >= deadline:
                raise ProbeError(
                    "probe_timeout", "loopback connection timed out"
                ) from None
            continue
    raise ProbeError("loopback_connect_failed", "loopback connection failed")


def _validated_unix_socket(path: Path) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or Path(os.path.normpath(os.fspath(path))) != path
    ):
        raise ProbeError("unsafe_unix_socket", "Unix socket path is unsafe")
    current = Path("/")
    try:
        for component in path.parts[1:-1]:
            current /= component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ProbeError("unsafe_unix_socket", "Unix socket ancestor is unsafe")
            writable = bool(metadata.st_mode & 0o022)
            sticky_root = bool(metadata.st_mode & stat.S_ISVTX) and metadata.st_uid == 0
            if metadata.st_uid not in {0, os.getuid()} or (
                writable and not sticky_root
            ):
                raise ProbeError("unsafe_unix_socket", "Unix socket ancestor is unsafe")
        socket_metadata = path.lstat()
    except ProbeError:
        raise
    except OSError:
        raise ProbeError("unsafe_unix_socket", "Unix socket is unavailable") from None
    if (
        stat.S_ISLNK(socket_metadata.st_mode)
        or not stat.S_ISSOCK(socket_metadata.st_mode)
        or socket_metadata.st_uid not in {0, os.getuid()}
        or socket_metadata.st_mode & 0o022
    ):
        raise ProbeError("unsafe_unix_socket", "Unix socket is unsafe")
    return path


def _connect_unix_socket(path: Path, deadline: float) -> socket.socket:
    before = path.lstat()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(_remaining(deadline))
        connection.connect(str(path))
        after = path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ProbeError("unsafe_unix_socket", "Unix socket changed during connect")
        if hasattr(socket, "SO_PEERCRED"):
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            if uid not in {0, os.getuid()}:
                raise ProbeError("unsafe_unix_peer", "Unix socket peer is untrusted")
        return connection
    except ProbeError:
        connection.close()
        raise
    except (OSError, TimeoutError):
        connection.close()
        if time.monotonic() >= deadline:
            raise ProbeError(
                "probe_timeout", "Unix socket connection timed out"
            ) from None
        raise ProbeError(
            "loopback_connect_failed", "Unix socket connection failed"
        ) from None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeError("probe_timeout", "probe deadline expired")
    return remaining


def _set_connection_timeout(connection: socket.socket, deadline: float) -> None:
    remaining = _remaining(deadline)
    if connection.fileno() >= 0:
        connection.settimeout(remaining)


def _read_response_body(
    response: http.client.HTTPResponse,
    connection: socket.socket,
    deadline: float,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    captured = 0
    while captured <= max_bytes:
        _set_connection_timeout(connection, deadline)
        chunk = response.read1(min(65_536, max_bytes + 1 - captured))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        captured += len(chunk)
        if captured > max_bytes:
            raise ProbeError(
                "response_too_large", "loopback response exceeds byte limit"
            )
    raise ProbeError("response_too_large", "loopback response exceeds byte limit")


def _reject_json_constant() -> None:
    raise ValueError("non-finite JSON constant")


class _HeaderLimitExceeded(Exception):
    pass


class _SocketDeadlineGuard:
    def __init__(self, connection: socket.socket, deadline: float) -> None:
        self._connection = connection.dup()
        self._done = threading.Event()
        self.expired = False
        self._shutdown_failed = False
        self._thread = threading.Thread(
            target=self._watch,
            args=(deadline,),
            name="local-agent-loopback-deadline",
            daemon=True,
        )
        self._thread.start()

    def _watch(self, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        if not self._done.wait(remaining):
            self.expired = True
            try:
                self._connection.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                if error.errno not in {errno.ENOTCONN, errno.EBADF}:
                    self._shutdown_failed = True

    def close(self) -> None:
        self._done.set()
        failed = False
        try:
            self._connection.close()
        except OSError:
            failed = True
        self._thread.join(timeout=0.1)
        if self._thread.is_alive() or self._shutdown_failed or failed:
            raise ProbeError(
                "loopback_cleanup_failed", "loopback deadline cleanup failed"
            )


class _HeaderBoundedFile:
    def __init__(self, wrapped: Any, limit: int) -> None:
        self._wrapped = wrapped
        self._limit = limit
        self._header_bytes = 0
        self._in_headers = True

    def readline(self, size: int = -1) -> bytes:
        if self._in_headers:
            remaining = self._limit + 1 - self._header_bytes
            if remaining <= 0:
                raise _HeaderLimitExceeded
            effective_size = remaining if size < 0 else min(size, remaining)
            line = cast(bytes, self._wrapped.readline(effective_size))
            self._header_bytes += len(line)
            if self._header_bytes > self._limit:
                raise _HeaderLimitExceeded
            if line in (b"\r\n", b"\n"):
                self._in_headers = False
            return line
        return cast(bytes, self._wrapped.readline(size))

    def read(self, size: int = -1) -> bytes:
        return cast(bytes, self._wrapped.read(size))

    def close(self) -> None:
        self._wrapped.close()

    @property
    def closed(self) -> bool:
        return bool(self._wrapped.closed)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class _HeaderBoundedSocket:
    def __init__(self, wrapped: socket.socket, limit: int) -> None:
        self._wrapped = wrapped
        self._limit = limit

    def makefile(self, *args: Any, **kwargs: Any) -> _HeaderBoundedFile:
        return _HeaderBoundedFile(self._wrapped.makefile(*args, **kwargs), self._limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


__all__ = [
    "ConfigError",
    "LoopbackResponse",
    "ProbeError",
    "ProcessResult",
    "TrustedExecutableResolver",
    "build_probe_environment",
    "get_loopback_json",
    "get_loopback_json_zero_write",
    "run_bounded_process",
    "run_zero_write_process",
]
