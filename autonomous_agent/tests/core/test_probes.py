from __future__ import annotations

import contextlib
import errno
import http.server
import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import autonomous_agent.core.probes as probes_module
from autonomous_agent.core.probes import (
    ProbeError,
    TrustedExecutableResolver,
    build_probe_environment,
    get_loopback_json,
    run_bounded_process,
)


def _write_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _resolver_for(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> TrustedExecutableResolver:
    root.chmod(0o755)
    monkeypatch.setattr(probes_module, "_SYSTEM_EXECUTABLE_ROOTS", ())
    monkeypatch.setattr(probes_module, "_DEFAULT_HOMEBREW_PREFIX", root.parent)
    return TrustedExecutableResolver()


@pytest.fixture
def resolver_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix=".resolver-test-", dir=Path.cwd()
    ) as directory:
        yield Path(directory)


def _system_executable(name: str) -> Path:
    return TrustedExecutableResolver().resolve(name)


def _pid_exists(pid: int, identity: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    command_line = Path(f"/proc/{pid}/cmdline")
    with contextlib.suppress(OSError):
        if identity.encode() not in command_line.read_bytes():
            return False
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        with contextlib.suppress(OSError):
            return "State:\tZ" not in status.read_text(encoding="utf-8")
    return True


def _assert_pid_gone(pid: int, identity: str) -> None:
    deadline = time.monotonic() + 2.0
    while _pid_exists(pid, identity) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _pid_exists(pid, identity)


def _process_with_marker_exists(marker: str) -> bool:
    encoded = marker.encode()
    for command_line in Path("/proc").glob("[0-9]*/cmdline"):
        with contextlib.suppress(OSError):
            if encoded in command_line.read_bytes():
                return True
    return False


def _metadata_with_uid(metadata: os.stat_result, uid: int) -> os.stat_result:
    values = list(metadata)
    values[4] = uid
    return os.stat_result(values)


def _assert_clean_error(error: ProbeError, secret: str) -> None:
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


class _HostileFloat(float):
    def __float__(self) -> float:
        raise RuntimeError("secret deadline accessor")


def test_resolver_ignores_project_and_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_bin = tmp_path / "project"
    project_bin.mkdir()
    _write_executable(project_bin / "hostile-probe")
    monkeypatch.chdir(project_bin)
    monkeypatch.setenv("PATH", f"{project_bin}:/usr/bin")

    with pytest.raises(ProbeError, match=r"^executable_not_found:"):
        TrustedExecutableResolver().resolve("hostile-probe")


@pytest.mark.parametrize("name", ["../python3", "/usr/bin/python3", "", "a/b"])
def test_resolver_rejects_non_names(name: str) -> None:
    with pytest.raises(ProbeError, match=r"^invalid_executable_name:"):
        TrustedExecutableResolver().resolve(name)


def test_resolver_returns_trusted_system_alias_with_canonical_target() -> None:
    resolved = TrustedExecutableResolver().resolve("python3")

    assert resolved.is_absolute()
    assert resolved.name == "python3"
    assert resolved.resolve(strict=True).is_file()


def test_resolver_preserves_git_alias_identity_and_runner_hardens_target(
    resolver_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = resolver_root / "prefix"
    bin_root = prefix / "bin"
    bin_root.mkdir(parents=True)
    real_git = TrustedExecutableResolver().resolve("git").resolve(strict=True)
    git_target = bin_root / "git-real"
    _write_executable(git_target, f'#!/bin/sh\nexec {real_git} "$@"\n')
    git_alias = bin_root / "git"
    git_alias.symlink_to(git_target.name)
    resolver = _resolver_for(monkeypatch, bin_root)

    repository = tmp_path / "repository"
    repository.mkdir()
    fsmonitor_marker = tmp_path / "fsmonitor-ran"
    hook_marker = tmp_path / "hook-ran"
    alias_marker = tmp_path / "alias-ran"
    hostile = tmp_path / "hostile-fsmonitor.sh"
    _write_executable(hostile, f"#!/bin/sh\ntouch {fsmonitor_marker}\n")
    subprocess.run(
        [real_git, "-C", repository, "init", "-q"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    hooks = tmp_path / "hostile-hooks"
    hooks.mkdir()
    _write_executable(hooks / "post-index-change", f"#!/bin/sh\ntouch {hook_marker}\n")
    with (repository / ".git/config").open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n[core]\n\tfsmonitor = {hostile}\n\thooksPath = {hooks}\n"
            f"[alias]\n\thostile-alias = !touch {alias_marker}\n"
        )

    resolved = resolver.resolve("git")
    result = run_bounded_process(
        resolved,
        ("-C", str(repository), "status", "--porcelain=v1"),
        {},
        time.monotonic() + 3.0,
        8_192,
    )

    assert result.returncode == 0
    with pytest.raises(ProbeError, match=r"^unsafe_git_arguments:"):
        run_bounded_process(
            resolved,
            ("-C", str(repository), "hostile-alias"),
            {},
            time.monotonic() + 3.0,
            8_192,
        )
    assert not fsmonitor_marker.exists()
    assert not hook_marker.exists()
    assert not alias_marker.exists()
    assert resolved == git_alias
    assert resolved.resolve(strict=True) == git_target


def test_direct_path_cannot_forge_resolver_evidence() -> None:
    resolved = TrustedExecutableResolver().resolve("git")

    with pytest.raises(ProbeError, match=r"^untrusted_executable:"):
        run_bounded_process(
            Path(os.fspath(resolved)),
            ("--version",),
            {},
            time.monotonic() + 2.0,
            4_096,
        )


def test_homebrew_bin_link_may_target_validated_cellar(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = resolver_root / "prefix"
    bin_root = prefix / "bin"
    cellar_bin = prefix / "Cellar/node/1.0/bin"
    bin_root.mkdir(parents=True)
    cellar_bin.mkdir(parents=True)
    target = cellar_bin / "node-real"
    shutil.copy2(TrustedExecutableResolver().resolve("true").resolve(), target)
    target.chmod(0o755)
    alias = bin_root / "node"
    alias.symlink_to(Path("../Cellar/node/1.0/bin/node-real"))
    resolver = _resolver_for(monkeypatch, bin_root)

    resolved = resolver.resolve("node")

    assert resolved == alias
    assert resolved.resolve(strict=True) == target


def test_installed_homebrew_links_resolve_when_present() -> None:
    resolver = TrustedExecutableResolver()
    checked = 0
    for name in ("gh", "node", "npm", "uv"):
        candidate = Path("/home/linuxbrew/.linuxbrew/bin") / name
        if not os.path.lexists(candidate):
            continue
        checked += 1
        resolved = resolver.resolve(name)
        assert resolved.name == name
        assert resolved.parent == Path("/home/linuxbrew/.linuxbrew").resolve() / "bin"
        assert resolved.resolve(strict=True) == candidate.resolve(strict=True)
        assert resolved.resolve(strict=True).is_file()
    if checked == 0:
        pytest.skip("no selected Homebrew executable is installed")


def test_resolver_rejects_symlink_escape(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    outside = resolver_root / "outside"
    trusted.mkdir(parents=True)
    outside.mkdir()
    _write_executable(outside / "probe")
    (trusted / "probe").symlink_to(outside / "probe")
    resolver = _resolver_for(monkeypatch, trusted)

    with pytest.raises(ProbeError, match=r"^untrusted_executable:"):
        resolver.resolve("probe")


@pytest.mark.parametrize("mode", [0o775, 0o777])
def test_resolver_rejects_writable_approved_root(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    trusted.mkdir(parents=True)
    _write_executable(trusted / "probe")
    resolver = _resolver_for(monkeypatch, trusted)
    trusted.chmod(mode)

    with pytest.raises(ProbeError, match=r"^untrusted_executable_root:"):
        resolver.resolve("probe")


def test_resolver_rejects_writable_homebrew_prefix_ancestor(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    trusted.mkdir(parents=True)
    _write_executable(trusted / "probe")
    resolver = _resolver_for(monkeypatch, trusted)
    resolver_root.chmod(0o777)

    with pytest.raises(ProbeError, match=r"^untrusted_executable_root:"):
        resolver.resolve("probe")


def test_resolver_rejects_writable_executable(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    trusted.mkdir(parents=True)
    executable = trusted / "probe"
    _write_executable(executable)
    resolver = _resolver_for(monkeypatch, trusted)
    executable.chmod(0o777)

    with pytest.raises(ProbeError, match=r"^untrusted_executable:"):
        resolver.resolve("probe")


def test_resolver_rejects_writable_executable_ancestor(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    nested = trusted / "nested"
    nested.mkdir(parents=True)
    _write_executable(nested / "probe")
    (trusted / "probe").symlink_to(nested / "probe")
    resolver = _resolver_for(monkeypatch, trusted)
    nested.chmod(0o775)

    with pytest.raises(ProbeError, match=r"^untrusted_executable:"):
        resolver.resolve("probe")


def test_resolver_rejects_wrong_owner(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    trusted.mkdir(parents=True)
    _write_executable(trusted / "probe")
    resolver = _resolver_for(monkeypatch, trusted)
    real_fstat = os.fstat

    def wrong_owner_fstat(descriptor: int) -> os.stat_result:
        return _metadata_with_uid(real_fstat(descriptor), os.getuid() + 10_000)

    monkeypatch.setattr(probes_module.os, "fstat", wrong_owner_fstat)

    with pytest.raises(ProbeError, match=r"^untrusted_executable:"):
        resolver.resolve("probe")


def test_resolver_rejects_wrong_owner_executable_ancestor(
    resolver_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = resolver_root / "prefix" / "bin"
    nested = trusted / "nested"
    nested.mkdir(parents=True)
    _write_executable(nested / "probe")
    (trusted / "probe").symlink_to(nested / "probe")
    resolver = _resolver_for(monkeypatch, trusted)
    real_lstat = Path.lstat

    def wrong_owner_lstat(path: Path) -> os.stat_result:
        metadata = real_lstat(path)
        if path == nested:
            return _metadata_with_uid(metadata, os.getuid() + 10_000)
        return metadata

    monkeypatch.setattr(Path, "lstat", wrong_owner_lstat)

    with pytest.raises(ProbeError, match=r"^untrusted_executable:"):
        resolver.resolve("probe")


def test_resolver_open_failure_has_no_raw_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.EIO, "secret-open-path")

    monkeypatch.setattr(probes_module.os, "open", fail_open)
    with pytest.raises(ProbeError) as captured:
        TrustedExecutableResolver().resolve("python3")

    _assert_clean_error(captured.value, "secret-open-path")


def test_resolver_stat_failure_has_no_raw_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_lstat = Path.lstat

    def fail_candidate_lstat(path: Path) -> os.stat_result:
        if path.name == "python3":
            raise OSError(errno.EIO, "secret-stat-path")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_candidate_lstat)
    with pytest.raises(ProbeError) as captured:
        TrustedExecutableResolver().resolve("python3")

    _assert_clean_error(captured.value, "secret-stat-path")


def test_resolver_descriptor_close_failure_is_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = os.close

    def fail_close(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError(errno.EIO, "secret-descriptor-path")

    monkeypatch.setattr(probes_module.os, "close", fail_close)
    with pytest.raises(ProbeError, match=r"^untrusted_executable:") as captured:
        TrustedExecutableResolver().resolve("python3")

    _assert_clean_error(captured.value, "secret-descriptor-path")


def test_probe_environment_is_minimal_and_strips_injection(tmp_path: Path) -> None:
    hostile = {
        "PATH": str(tmp_path),
        "LD_PRELOAD": "/secret/loader.so",
        "LD_LIBRARY_PATH": "/secret/lib",
        "PYTHONPATH": "/secret/python",
        "PYTHONHOME": "/secret/home",
        "BASH_ENV": "/secret/bashrc",
        "ENV": "/secret/env",
        "SHELLOPTS": "xtrace",
        "HTTP_PROXY": "http://attacker.invalid",
        "HTTPS_PROXY": "http://attacker.invalid",
        "ALL_PROXY": "socks5://attacker.invalid",
        "NO_PROXY": "*",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "alias.status",
        "GIT_CONFIG_VALUE_0": "!echo compromised",
        "HOME": str(tmp_path),
        "TOKEN": "must-not-cross",
    }

    assert build_probe_environment("python3", hostile) == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def test_git_environment_contains_only_forced_config() -> None:
    environment = build_probe_environment(
        "git",
        {
            "HOME": "/attacker",
            "GIT_CONFIG_SYSTEM": "/attacker/system",
            "GIT_CONFIG_GLOBAL": "/attacker/global",
            "GIT_CONFIG_COUNT": "1",
        },
    )

    assert environment == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_runtime_environment_forwards_only_validated_runtime_values(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)

    environment = build_probe_environment(
        "podman",
        {
            "XDG_RUNTIME_DIR": str(runtime),
            "HOME": str(tmp_path),
            "HTTP_PROXY": "http://attacker.invalid",
        },
    )

    assert environment == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": str(runtime),
    }


def test_runtime_environment_rejects_unsafe_runtime_directory(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)

    with pytest.raises(ProbeError, match=r"^unsafe_environment:"):
        build_probe_environment("podman", {"XDG_RUNTIME_DIR": str(runtime)})


def test_runtime_environment_rejects_symlinked_runtime_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    runtime = real_parent / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ProbeError, match=r"^unsafe_environment:"):
        build_probe_environment("podman", {"XDG_RUNTIME_DIR": str(alias / "runtime")})


def test_runtime_environment_accepts_bus_only_inside_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    bus = runtime / "bus"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(bus))
    try:
        environment = build_probe_environment(
            "systemd-run",
            {
                "XDG_RUNTIME_DIR": str(runtime),
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
            },
        )
    finally:
        listener.close()

    assert environment["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={bus}"


def test_runtime_environment_rejects_bus_parent_traversal(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    outside = tmp_path / "outside.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(outside))
    try:
        with pytest.raises(ProbeError, match=r"^unsafe_environment:"):
            build_probe_environment(
                "systemd-run",
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "DBUS_SESSION_BUS_ADDRESS": (
                        f"unix:path={runtime / '..' / outside.name}"
                    ),
                },
            )
    finally:
        listener.close()


def test_process_rejects_expired_deadline_without_starting(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    python = _system_executable("python3")

    with pytest.raises(ProbeError, match=r"^deadline_expired:"):
        run_bounded_process(
            python,
            ("-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
            {},
            time.monotonic() - 1.0,
            128,
        )

    assert not marker.exists()


def test_process_rechecks_deadline_after_executable_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started"
    python = _system_executable("python3")
    original_validator = probes_module._validated_explicit_executable

    def delayed_validator(executable: Path) -> Path:
        validated = original_validator(executable)
        time.sleep(0.05)
        return validated

    monkeypatch.setattr(
        probes_module, "_validated_explicit_executable", delayed_validator
    )

    with pytest.raises(ProbeError, match=r"^deadline_expired:"):
        run_bounded_process(
            python,
            ("-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
            {},
            time.monotonic() + 0.02,
            128,
        )

    assert not marker.exists()


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1_048_577])
def test_process_rejects_invalid_byte_budget(max_bytes: int) -> None:
    with pytest.raises(ProbeError, match=r"^invalid_max_bytes:"):
        run_bounded_process(
            _system_executable("python3"),
            ("-c", "pass"),
            {},
            time.monotonic() + 1.0,
            max_bytes,
        )


@pytest.mark.parametrize(
    "deadline",
    [True, float("nan"), float("inf"), "tomorrow", _HostileFloat(1.0)],
)
def test_process_rejects_invalid_deadline(deadline: object) -> None:
    with pytest.raises(ProbeError, match=r"^invalid_deadline:"):
        run_bounded_process(
            _system_executable("python3"),
            ("-c", "pass"),
            {},
            deadline,  # type: ignore[arg-type] - hostile runtime input
            128,
        )


def test_process_converts_finite_deadline_overflow_to_stable_error() -> None:
    with pytest.raises(ProbeError, match=r"^invalid_deadline:") as captured:
        run_bounded_process(
            _system_executable("python3"),
            ("-c", "import time; time.sleep(1)"),
            {},
            1e308,
            128,
        )

    _assert_clean_error(captured.value, "secret")


def test_process_uses_sanitized_environment() -> None:
    python = _system_executable("python3")
    code = (
        "import json,os; print(json.dumps({k: os.environ.get(k) for k in "
        "['PATH','LD_PRELOAD','PYTHONPATH','BASH_ENV','HTTP_PROXY']}))"
    )

    result = run_bounded_process(
        python,
        ("-c", code),
        {
            "PATH": "/attacker",
            "LD_PRELOAD": "/attacker/lib.so",
            "PYTHONPATH": "/attacker/python",
            "BASH_ENV": "/attacker/bashrc",
            "HTTP_PROXY": "http://attacker.invalid",
        },
        time.monotonic() + 3.0,
        4_096,
    )

    assert result.returncode == 0
    assert result.timed_out is False
    assert result.truncated is False
    assert json.loads(result.stdout) == {
        "PATH": None,
        "LD_PRELOAD": None,
        "PYTHONPATH": None,
        "BASH_ENV": None,
        "HTTP_PROXY": None,
    }


@pytest.mark.parametrize("limit", [1, 1_024, 4_096])
def test_process_combined_output_never_exceeds_cap(limit: int) -> None:
    python = _system_executable("python3")
    code = "import os; os.write(1, b'o' * 8192); os.write(2, b'e' * 8192)"

    result = run_bounded_process(
        python,
        ("-c", code),
        {},
        time.monotonic() + 3.0,
        limit,
    )

    assert result.truncated is True
    assert result.timed_out is False
    assert len(result.stdout) + len(result.stderr) == limit


def test_output_cap_terminates_entire_process_group(tmp_path: Path) -> None:
    python = _system_executable("python3")
    child_pid_file = tmp_path / "child.pid"
    code = "\n".join(
        [
            "import os, pathlib, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "read_fd, write_fd = os.pipe()",
            "child = os.fork()",
            "if child == 0:",
            "    os.close(write_fd)",
            "    os.read(read_fd, 1)",
            "    while True: os.write(1, b'x' * 4096)",
            "os.close(read_fd)",
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child))",
            "os.write(write_fd, b'x')",
            "os.close(write_fd)",
            "while True: os.write(2, b'y' * 4096)",
        ]
    )

    result = run_bounded_process(
        python,
        ("-c", code),
        {},
        time.monotonic() + 5.0,
        2_048,
    )

    assert result.truncated is True
    assert child_pid_file.exists()
    _assert_pid_gone(
        int(child_pid_file.read_text(encoding="utf-8")), str(child_pid_file)
    )


def test_timeout_terminates_entire_process_group(tmp_path: Path) -> None:
    python = _system_executable("python3")
    child_pid_file = tmp_path / "child.pid"
    code = "\n".join(
        [
            "import os, pathlib, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "child = os.fork()",
            "if child == 0:",
            "    while True: time.sleep(1)",
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child))",
            "while True: time.sleep(1)",
        ]
    )

    result = run_bounded_process(
        python,
        ("-c", code),
        {},
        time.monotonic() + 0.35,
        2_048,
    )

    assert result.timed_out is True
    assert result.truncated is False
    assert child_pid_file.exists()
    _assert_pid_gone(
        int(child_pid_file.read_text(encoding="utf-8")), str(child_pid_file)
    )


def _stubborn_group_code(identity: Path) -> str:
    return "\n".join(
        [
            "import os, pathlib, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            "child = os.fork()",
            "if child == 0:",
            "    while True: time.sleep(1)",
            f"pathlib.Path({str(identity)!r}).write_text(f'{{os.getpid()}}:{{child}}')",
            "while True: time.sleep(1)",
        ]
    )


def _cleanup_injected_group(identity: Path, real_killpg: object) -> None:
    if not identity.exists():
        return
    group_text, child_text = identity.read_text(encoding="utf-8").split(":")
    group_id = int(group_text)
    child_id = int(child_text)
    with contextlib.suppress(ProcessLookupError):
        real_killpg(group_id, signal.SIGKILL)  # type: ignore[operator]
    with contextlib.suppress(ChildProcessError):
        os.waitpid(group_id, 0)
    _assert_pid_gone(child_id, str(identity))


def test_termination_surfaces_non_benign_term_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "term-failure.pid"
    real_killpg = os.killpg

    def fail_term(group_id: int, requested_signal: int) -> None:
        if requested_signal == signal.SIGTERM:
            raise PermissionError(errno.EPERM, "secret-term-path")
        real_killpg(group_id, requested_signal)

    monkeypatch.setattr(probes_module.os, "killpg", fail_term)
    try:
        with pytest.raises(ProbeError, match=r"^termination_failed:") as captured:
            run_bounded_process(
                _system_executable("python3"),
                ("-c", _stubborn_group_code(identity)),
                {},
                time.monotonic() + 0.25,
                1_024,
            )
        _assert_clean_error(captured.value, "secret-term-path")
    finally:
        _cleanup_injected_group(identity, real_killpg)


def test_termination_surfaces_kill_failure_and_test_cleans_survivor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "kill-failure.pid"
    real_killpg = os.killpg

    def fail_kill(group_id: int, requested_signal: int) -> None:
        if requested_signal == signal.SIGKILL:
            raise PermissionError(errno.EPERM, "secret-kill-path")
        real_killpg(group_id, requested_signal)

    monkeypatch.setattr(probes_module.os, "killpg", fail_kill)
    try:
        with pytest.raises(ProbeError, match=r"^termination_failed:") as captured:
            run_bounded_process(
                _system_executable("python3"),
                ("-c", _stubborn_group_code(identity)),
                {},
                time.monotonic() + 0.25,
                1_024,
            )
        _assert_clean_error(captured.value, "secret-kill-path")
    finally:
        _cleanup_injected_group(identity, real_killpg)


def test_termination_surfaces_wait_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "wait-failure.pid"
    real_wait = subprocess.Popen.wait
    failed = False

    def fail_wait(
        process: subprocess.Popen[bytes], timeout: float | None = None
    ) -> int:
        nonlocal failed
        if str(identity) in " ".join(str(item) for item in process.args) and not failed:
            failed = True
            raise OSError(errno.EIO, "secret-wait-path")
        return real_wait(process, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fail_wait)
    with pytest.raises(ProbeError, match=r"^termination_failed:") as captured:
        run_bounded_process(
            _system_executable("python3"),
            ("-c", _stubborn_group_code(identity)),
            {},
            time.monotonic() + 0.25,
            1_024,
        )

    _assert_clean_error(captured.value, "secret-wait-path")


def test_selector_close_failure_is_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_factory = selectors.DefaultSelector

    class CloseFailingSelector:
        def __init__(self) -> None:
            self._wrapped = real_factory()

        def close(self) -> None:
            self._wrapped.close()
            raise OSError(errno.EIO, "secret-selector-path")

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    monkeypatch.setattr(
        probes_module.selectors, "DefaultSelector", CloseFailingSelector
    )
    with pytest.raises(ProbeError, match=r"^process_cleanup_failed:") as captured:
        run_bounded_process(
            _system_executable("python3"),
            ("-c", "pass"),
            {},
            time.monotonic() + 2.0,
            1_024,
        )

    _assert_clean_error(captured.value, "secret-selector-path")


def test_git_probe_disables_repository_fsmonitor_and_aliases(tmp_path: Path) -> None:
    git = _system_executable("git")
    repository = tmp_path / "repository"
    repository.mkdir()
    marker = tmp_path / "executed"
    hostile = tmp_path / "hostile.sh"
    _write_executable(hostile, f"#!/bin/sh\ntouch {marker}\n")
    subprocess.run(
        [git, "-C", repository, "init", "-q"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    config = repository / ".git/config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n[core]\n\tfsmonitor = {hostile}\n[alias]\n\tstatus = !touch {marker}\n"
        )

    result = run_bounded_process(
        git,
        ("-C", str(repository), "status", "--porcelain=v1"),
        build_probe_environment("git", {}),
        time.monotonic() + 3.0,
        8_192,
    )

    assert result.returncode == 0
    assert marker.exists() is False


def test_git_probe_rejects_non_builtin_or_mutating_invocation() -> None:
    git = _system_executable("git")

    with pytest.raises(ProbeError, match=r"^unsafe_git_arguments:"):
        run_bounded_process(
            git,
            ("hostile-alias",),
            build_probe_environment("git", {}),
            time.monotonic() + 1.0,
            1_024,
        )


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/slow":
            body = b'{"slow":"streamed-body"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for byte in body:
                try:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.03)
            return
        if self.path == "/large-header":
            self.send_response(200)
            self.send_header("X-Fill", "x" * 8_192)
            self.send_header("Content-Length", "2")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(b"{}")
            return
        if self.path == "/large":
            body = json.dumps({"value": "x" * 8_192}).encode()
        else:
            body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _IPv6HTTPServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


@contextlib.contextmanager
def _http_server(
    host: str,
) -> Iterator[tuple[http.server.ThreadingHTTPServer, str]]:
    server_type = _IPv6HTTPServer if ":" in host else http.server.ThreadingHTTPServer
    server = server_type((host, 0), _JsonHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        endpoint = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
        yield server, endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2"])
def test_loopback_http_accepts_all_ipv4_loopback(host: str) -> None:
    with _http_server(host) as (_server, endpoint):
        response = get_loopback_json(endpoint, "/health", time.monotonic() + 3.0, 4_096)

    assert response.status_code == 200
    assert response.data == {"ok": True, "path": "/health"}
    assert response.body_bytes > 0


def test_loopback_http_accepts_ipv6_loopback() -> None:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is unavailable")
    try:
        manager = _http_server("::1")
        server_and_endpoint = manager.__enter__()
    except OSError:
        pytest.skip("IPv6 loopback cannot bind")
    try:
        _server, endpoint = server_and_endpoint
        response = get_loopback_json(endpoint, "/health", time.monotonic() + 3.0, 4_096)
    finally:
        manager.__exit__(None, None, None)

    assert response.status_code == 200
    assert response.data == {"ok": True, "path": "/health"}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://8.8.8.8:80",
        "http://10.0.0.1:80",
        "http://192.168.1.1:80",
        "https://127.0.0.1:443",
        "ftp://127.0.0.1/resource",
        "http://user:password@127.0.0.1:80",
    ],
)
def test_loopback_http_rejects_non_loopback_schemes_and_credentials(
    endpoint: str,
) -> None:
    with pytest.raises(ProbeError, match=r"^(non_loopback_endpoint|invalid_endpoint):"):
        get_loopback_json(endpoint, "/", time.monotonic() + 1.0, 1_024)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:",
        "http://127.0.0.1:0",
        "http://127.0.0.1:-1",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:not-a-port",
        "http://[::1]:",
        "http://[::1]:0",
        "http://[::1]:-1",
        "http://[::1]:65536",
        "http://[::1]:not-a-port",
        "http://localhost:",
        "http://localhost:0",
        "http://localhost:-1",
        "http://localhost:65536",
        "http://localhost:not-a-port",
    ],
)
def test_loopback_http_rejects_explicit_invalid_ports(endpoint: str) -> None:
    with pytest.raises(ProbeError, match=r"^invalid_endpoint:"):
        get_loopback_json(endpoint, "/", time.monotonic() + 1.0, 1_024)


def test_loopback_http_rejects_mixed_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        probes_module,
        "_RESOLVER_HELPER_CODE",
        "import sys; sys.stdout.write("
        '\'[[2,1,6,["127.0.0.1",80]],[2,1,6,["8.8.8.8",80]]]\')',
    )

    with pytest.raises(ProbeError, match=r"^non_loopback_endpoint:"):
        get_loopback_json("http://localhost:80", "/", time.monotonic() + 1.0, 1_024)


def test_loopback_http_rejects_arbitrary_hostname_even_if_dns_claims_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
    ]
    monkeypatch.setattr(probes_module.socket, "getaddrinfo", lambda *args: answers)

    with pytest.raises(ProbeError, match=r"^non_loopback_endpoint:"):
        get_loopback_json(
            "http://attacker.invalid:80",
            "/",
            time.monotonic() + 1.0,
            1_024,
        )


def test_loopback_http_bounds_name_resolution_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    resolver_marker = "local-agent-blocked-resolver-marker"

    def delayed_resolution(*_args: object) -> list[object]:
        release.wait(2.0)
        return []

    monkeypatch.setattr(probes_module.socket, "getaddrinfo", delayed_resolution)
    monkeypatch.setattr(
        probes_module,
        "_RESOLVER_HELPER_CODE",
        f"import time\n# {resolver_marker}\ntime.sleep(60)",
        raising=False,
    )
    try:
        for _run in range(3):
            with pytest.raises(ProbeError, match=r"^probe_timeout:"):
                get_loopback_json(
                    "http://localhost:80",
                    "/",
                    time.monotonic() + 0.1,
                    1_024,
                )
            assert not any(
                thread.name == "local-agent-loopback-resolver"
                for thread in threading.enumerate()
            )
            assert not _process_with_marker_exists(resolver_marker)
    finally:
        release.set()


def test_loopback_http_does_not_use_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://8.8.8.8:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://8.8.8.8:9999")
    monkeypatch.setenv("ALL_PROXY", "socks5://8.8.8.8:9999")

    with _http_server("127.0.0.1") as (_server, endpoint):
        response = get_loopback_json(endpoint, "/health", time.monotonic() + 3.0, 4_096)

    assert response.status_code == 200


def test_loopback_socket_close_failure_is_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_close(_socket: object) -> None:
        raise OSError(errno.EIO, "secret-socket-path")

    monkeypatch.setattr(
        probes_module._HeaderBoundedSocket,
        "close",
        fail_close,
        raising=False,
    )
    with (
        _http_server("127.0.0.1") as (_server, endpoint),
        pytest.raises(ProbeError, match=r"^loopback_cleanup_failed:") as captured,
    ):
        get_loopback_json(endpoint, "/health", time.monotonic() + 3.0, 4_096)

    _assert_clean_error(captured.value, "secret-socket-path")


def test_loopback_http_rejects_redirects() -> None:
    with (
        _http_server("127.0.0.1") as (_server, endpoint),
        pytest.raises(ProbeError, match=r"^redirect_forbidden:"),
    ):
        get_loopback_json(endpoint, "/redirect", time.monotonic() + 3.0, 4_096)


def test_loopback_http_rejects_oversized_body() -> None:
    with (
        _http_server("127.0.0.1") as (_server, endpoint),
        pytest.raises(ProbeError, match=r"^response_too_large:"),
    ):
        get_loopback_json(endpoint, "/large", time.monotonic() + 3.0, 256)


def test_loopback_http_rejects_oversized_headers() -> None:
    with (
        _http_server("127.0.0.1") as (_server, endpoint),
        pytest.raises(ProbeError, match=r"^response_too_large:"),
    ):
        get_loopback_json(endpoint, "/large-header", time.monotonic() + 3.0, 256)


def test_loopback_http_enforces_one_deadline_across_streamed_body() -> None:
    started = time.monotonic()
    with (
        _http_server("127.0.0.1") as (_server, endpoint),
        pytest.raises(ProbeError, match=r"^probe_timeout:"),
    ):
        get_loopback_json(endpoint, "/slow", time.monotonic() + 0.15, 4_096)

    assert time.monotonic() - started < 0.6


def test_loopback_json_decode_cannot_overrun_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_loads = json.loads

    def delayed_loads(*args: object, **kwargs: object) -> object:
        result = real_loads(*args, **kwargs)
        time.sleep(0.12)
        return result

    monkeypatch.setattr(probes_module.json, "loads", delayed_loads)
    with (
        _http_server("127.0.0.1") as (_server, endpoint),
        pytest.raises(ProbeError, match=r"^probe_timeout:"),
    ):
        get_loopback_json(endpoint, "/health", time.monotonic() + 0.08, 4_096)


@contextlib.contextmanager
def _unix_http_server(socket_path: Path) -> Iterator[None]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    stopped = threading.Event()

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.recv(4_096)
                body = b'{"unix":true}'
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + body
                )
        finally:
            stopped.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        listener.close()
        stopped.wait(2.0)
        thread.join(timeout=2.0)


def test_loopback_accepts_validated_unix_socket() -> None:
    with tempfile.TemporaryDirectory(
        prefix=".probe-test-", dir=Path.cwd()
    ) as directory:
        socket_path = Path(directory) / "ollama.sock"
        with _unix_http_server(socket_path):
            response = get_loopback_json(
                socket_path, "/api/version", time.monotonic() + 3.0, 4_096
            )

    assert response.status_code == 200
    assert response.data == {"unix": True}


def test_loopback_rejects_symlinked_unix_socket() -> None:
    with tempfile.TemporaryDirectory(
        prefix=".probe-test-", dir=Path.cwd()
    ) as directory:
        root = Path(directory)
        target = root / "target.sock"
        link = root / "link.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(target))
        link.symlink_to(target)
        try:
            with pytest.raises(ProbeError, match=r"^unsafe_unix_socket:"):
                get_loopback_json(link, "/", time.monotonic() + 1.0, 1_024)
        finally:
            listener.close()


def test_loopback_rejects_invalid_request_path() -> None:
    with pytest.raises(ProbeError, match=r"^invalid_request_path:"):
        get_loopback_json(
            "http://127.0.0.1:80",
            "/health\r\nHost: attacker",
            time.monotonic() + 1.0,
            1_024,
        )


def test_loopback_rejects_expired_deadline_and_invalid_budget() -> None:
    with pytest.raises(ProbeError, match=r"^deadline_expired:"):
        get_loopback_json("http://127.0.0.1:80", "/", time.monotonic() - 1.0, 1_024)
    with pytest.raises(ProbeError, match=r"^invalid_max_bytes:"):
        get_loopback_json("http://127.0.0.1:80", "/", time.monotonic() + 1.0, 0)


def test_probe_errors_do_not_disclose_untrusted_values() -> None:
    secret = "very-secret-token"
    with pytest.raises(ProbeError) as captured:
        get_loopback_json(
            f"http://user:{secret}@127.0.0.1:80",
            "/",
            time.monotonic() + 1.0,
            1_024,
        )

    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
