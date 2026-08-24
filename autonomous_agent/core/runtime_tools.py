"""Trusted project tools implemented on the shared typed tool runtime."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from autonomous_agent.core.policy import NetworkKind, SideEffect
from autonomous_agent.core.tools import ExecutionContext, ToolRegistry, ToolSpec

_MAX_FILE_BYTES = 1_048_576
_MAX_ENTRIES = 2_000
_MAX_ARGUMENTS = 256
_TRUSTED_EXECUTABLE_ROOTS = (
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/local/bin"),
    Path("/home/linuxbrew/.linuxbrew/bin"),
)


class RuntimeToolError(RuntimeError):
    """A user-safe project tool failure."""


@dataclass(frozen=True)
class ReadFileInput:
    path: Path


@dataclass(frozen=True)
class ReadFileOutput:
    path: str
    content: str
    byte_size: int


@dataclass(frozen=True)
class WriteFileInput:
    path: Path
    content: str


@dataclass(frozen=True)
class WriteFileOutput:
    path: str
    byte_size: int


@dataclass(frozen=True)
class ListFilesInput:
    path: Path


@dataclass(frozen=True)
class ListFilesOutput:
    path: str
    entries: list[str]


@dataclass(frozen=True)
class RunProcessInput:
    argv: list[str]
    cwd: Path


@dataclass(frozen=True)
class RunProcessOutput:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str


class ProjectPathResolver:
    """Resolve non-symlink project targets once at the trust boundary."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve(strict=True)
        if not self.project_root.is_dir():
            raise RuntimeToolError("project root is not a directory")

    def resolve(self, candidate: Path, *, allow_missing: bool = False) -> Path:
        if not isinstance(candidate, Path):
            raise RuntimeToolError("tool path is invalid")
        lexical = candidate if candidate.is_absolute() else self.project_root / candidate
        lexical = Path(os.path.normpath(str(lexical)))
        if not lexical.is_absolute() or not lexical.is_relative_to(self.project_root):
            raise RuntimeToolError("tool path escapes the project")
        self._reject_symlink_chain(lexical, allow_missing=allow_missing)
        if allow_missing and not lexical.exists():
            missing: list[str] = []
            existing = lexical
            while not existing.exists():
                missing.append(existing.name)
                existing = existing.parent
            resolved = existing.resolve(strict=True).joinpath(*reversed(missing))
        else:
            resolved = lexical.resolve(strict=True)
        if not resolved.is_relative_to(self.project_root):
            raise RuntimeToolError("resolved tool path escapes the project")
        return resolved

    def _reject_symlink_chain(self, path: Path, *, allow_missing: bool) -> None:
        relative = path.relative_to(self.project_root)
        current = self.project_root
        for index, component in enumerate(relative.parts):
            current /= component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return
                raise RuntimeToolError("tool path parent does not exist") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeToolError("symlink tool paths are not permitted")


class ProjectToolRuntime:
    """Create bounded handlers while keeping policy in the shared registry."""

    def __init__(self, project_root: Path) -> None:
        self.paths = ProjectPathResolver(project_root)
        self.project_root = self.paths.project_root

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="project.read-file",
                version="1.0.0",
                description="Read one bounded regular file from the project.",
                input_type=ReadFileInput,
                output_type=ReadFileOutput,
                capabilities=frozenset({"project.read"}),
                side_effect=SideEffect.READ_ONLY,
                network=NetworkKind.NONE,
                requires_elevation=False,
                requires_recovery=False,
                default_timeout_s=5.0,
                max_output_bytes=_MAX_FILE_BYTES,
                handler=self.read_file,
                target_resolver=lambda item: (item.path,),
            )
        )
        registry.register(
            ToolSpec(
                name="project.write-file",
                version="1.0.0",
                description="Atomically write one bounded project file.",
                input_type=WriteFileInput,
                output_type=WriteFileOutput,
                capabilities=frozenset({"project.write"}),
                side_effect=SideEffect.WRITE_PROJECT,
                network=NetworkKind.NONE,
                requires_elevation=False,
                requires_recovery=False,
                default_timeout_s=5.0,
                max_output_bytes=8_192,
                handler=self.write_file,
                target_resolver=lambda item: (item.path,),
            )
        )
        registry.register(
            ToolSpec(
                name="project.list-files",
                version="1.0.0",
                description="List bounded project-relative paths.",
                input_type=ListFilesInput,
                output_type=ListFilesOutput,
                capabilities=frozenset({"project.read"}),
                side_effect=SideEffect.READ_ONLY,
                network=NetworkKind.NONE,
                requires_elevation=False,
                requires_recovery=False,
                default_timeout_s=5.0,
                max_output_bytes=131_072,
                handler=self.list_files,
                target_resolver=lambda item: (item.path,),
            )
        )
        registry.register(
            ToolSpec(
                name="project.run-process",
                version="1.0.0",
                description="Run a command without a shell in a networkless sandbox.",
                input_type=RunProcessInput,
                output_type=RunProcessOutput,
                capabilities=frozenset({"process.run"}),
                side_effect=SideEffect.PROCESS,
                network=NetworkKind.NONE,
                requires_elevation=False,
                requires_recovery=False,
                default_timeout_s=120.0,
                max_output_bytes=1_048_576,
                handler=self.run_process,
                target_resolver=lambda item: (item.cwd,),
            )
        )
        return registry

    def read_file(
        self, request: ReadFileInput, context: ExecutionContext
    ) -> ReadFileOutput:
        del context
        path = self.paths.resolve(request.path)
        if not path.is_file():
            raise RuntimeToolError("read target is not a regular file")
        payload = path.read_bytes()
        if len(payload) > _MAX_FILE_BYTES:
            raise RuntimeToolError("read target exceeds the byte limit")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeToolError("read target is not UTF-8 text") from error
        return ReadFileOutput(
            path=path.relative_to(self.project_root).as_posix(),
            content=content,
            byte_size=len(payload),
        )

    def write_file(
        self, request: WriteFileInput, context: ExecutionContext
    ) -> WriteFileOutput:
        del context
        payload = request.content.encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise RuntimeToolError("write content exceeds the byte limit")
        path = self.paths.resolve(request.path, allow_missing=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return WriteFileOutput(
            path=path.relative_to(self.project_root).as_posix(),
            byte_size=len(payload),
        )

    def list_files(
        self, request: ListFilesInput, context: ExecutionContext
    ) -> ListFilesOutput:
        del context
        path = self.paths.resolve(request.path)
        if not path.is_dir():
            raise RuntimeToolError("list target is not a directory")
        entries: list[str] = []
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                continue
            entries.append(item.relative_to(self.project_root).as_posix())
            if len(entries) >= _MAX_ENTRIES:
                break
        return ListFilesOutput(
            path=path.relative_to(self.project_root).as_posix() or ".",
            entries=entries,
        )

    def run_process(
        self, request: RunProcessInput, context: ExecutionContext
    ) -> RunProcessOutput:
        cwd = self.paths.resolve(request.cwd)
        if not cwd.is_dir():
            raise RuntimeToolError("process working directory is invalid")
        if not request.argv or len(request.argv) > _MAX_ARGUMENTS:
            raise RuntimeToolError("process argument count is invalid")
        executable = _trusted_executable(request.argv[0], self.project_root)
        sandbox_argv = sandbox_command(
            self.project_root,
            cwd,
            executable,
            request.argv[1:],
        )
        timeout = max(0.001, context.deadline_monotonic - time.monotonic())
        try:
            result = subprocess.run(  # nosec B603
                sandbox_argv,
                cwd=self.project_root,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                shell=False,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeToolError("sandboxed process timed out") from error
        cap = context.schema_limits.max_output_bytes // 2
        return RunProcessOutput(
            argv=list(request.argv),
            exit_code=result.returncode,
            stdout=result.stdout[:cap].decode("utf-8", errors="replace"),
            stderr=result.stderr[:cap].decode("utf-8", errors="replace"),
        )


def _trusted_executable(command: str, project_root: Path) -> Path:
    if not command or "/" in command or "\x00" in command:
        raise RuntimeToolError("process executable must be a trusted command name")
    project_candidate = project_root / ".venv" / "bin" / command
    candidates = (project_candidate,) + tuple(root / command for root in _TRUSTED_EXECUTABLE_ROOTS)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        allowed_roots = (project_root / ".venv" / "bin",) + _TRUSTED_EXECUTABLE_ROOTS
        if (
            stat.S_ISREG(metadata.st_mode)
            and os.access(resolved, os.X_OK)
            and any(resolved.is_relative_to(root) for root in allowed_roots)
        ):
            return resolved
    discovered = shutil.which(command, path="/usr/bin:/bin:/usr/local/bin")
    if discovered is not None:
        return Path(discovered).resolve(strict=True)
    raise RuntimeToolError("process executable is unavailable")


def sandbox_command(
    project_root: Path,
    cwd: Path,
    executable: Path,
    arguments: list[str],
) -> list[str]:
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file():
        raise RuntimeToolError("bubblewrap is required for process execution")
    command = [
        str(bwrap),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
    ]
    for root in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
        if root.exists():
            command.extend(("--ro-bind", str(root), str(root)))
    brew_root = Path("/home/linuxbrew/.linuxbrew")
    if executable.is_relative_to(brew_root):
        command.extend(("--ro-bind", str(brew_root), str(brew_root)))
    command.extend(
        (
            "--bind",
            str(project_root),
            "/workspace",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # nosec B108
            "--tmpfs",
            "/etc",
            "--chdir",
            "/workspace/" + cwd.relative_to(project_root).as_posix(),
        )
    )
    sandbox_executable = executable
    if executable.is_relative_to(project_root):
        sandbox_executable = Path("/workspace") / executable.relative_to(project_root)
    command.extend((str(sandbox_executable), *arguments))
    return command


__all__ = [
    "ListFilesInput",
    "ListFilesOutput",
    "ProjectPathResolver",
    "ProjectToolRuntime",
    "ReadFileInput",
    "ReadFileOutput",
    "RunProcessInput",
    "RunProcessOutput",
    "RuntimeToolError",
    "WriteFileInput",
    "WriteFileOutput",
    "sandbox_command",
]
