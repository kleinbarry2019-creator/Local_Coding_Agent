"""Trusted capability discovery and bounded external-tool provisioning."""

from __future__ import annotations

import os
import stat
import subprocess  # nosec B404
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from autonomous_agent.core.runtime_tools import sandbox_command

_TRUSTED_CATALOG: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "git": MappingProxyType({"apt": "git", "dnf": "git", "brew": "git"}),
        "ruff": MappingProxyType({"apt": "ruff", "dnf": "ruff", "brew": "ruff"}),
        "shellcheck": MappingProxyType(
            {"apt": "shellcheck", "dnf": "ShellCheck", "brew": "shellcheck"}
        ),
        "shfmt": MappingProxyType({"apt": "shfmt", "dnf": "shfmt", "brew": "shfmt"}),
        "node": MappingProxyType({"apt": "nodejs", "dnf": "nodejs", "brew": "node"}),
        "npm": MappingProxyType({"apt": "npm", "dnf": "npm", "brew": "node"}),
        "ollama": MappingProxyType({"brew": "ollama"}),
    }
)


@dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    executable: Path | None
    source: str
    version: str | None


@dataclass(frozen=True)
class InstallResult:
    installed: bool
    capability: Capability
    diagnostic: str


class CapabilityRegistry:
    """Discover tool availability from fixed executable roots and a fixed catalog."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve(strict=True)
        self._capabilities: dict[str, Capability] = {}

    def discover(self, name: str) -> Capability:
        if not _valid_name(name):
            raise ValueError("capability name is invalid")
        path = _find_executable(name, self.project_root)
        project_local = path is not None and path.is_relative_to(self.project_root)
        capability = Capability(
            name=name,
            available=path is not None,
            executable=path,
            source=(
                "project-sandbox"
                if project_local
                else "local"
                if path is not None
                else "missing"
            ),
            version=(
                _sandboxed_version(path, self.project_root)
                if project_local and path is not None
                else _version(path)
                if path is not None
                else None
            ),
        )
        self._capabilities[name] = capability
        return capability

    def ensure(self, name: str) -> InstallResult:
        current = self.discover(name)
        if current.available:
            return InstallResult(True, current, "already-available")
        recipe = _installation_recipe(name)
        if recipe is None:
            return InstallResult(False, current, "untrusted-or-unsupported-tool")
        result = PrivilegedSystemExecutor().install(recipe)
        verified = self.discover(name)
        return InstallResult(
            result == 0 and verified.available,
            verified,
            "installed-and-verified"
            if result == 0 and verified.available
            else "installation-or-verification-failed",
        )

    def snapshot(self) -> tuple[Capability, ...]:
        return tuple(self._capabilities[name] for name in sorted(self._capabilities))


@dataclass(frozen=True)
class InstallationRecipe:
    tool: str
    manager: str
    package: str
    command: tuple[str, ...]
    requires_elevation: bool


class PrivilegedSystemExecutor:
    """Spawn one allowlisted elevated child; never keep a root agent process."""

    def install(self, recipe: InstallationRecipe) -> int:
        expected = _installation_recipe(recipe.tool)
        if expected != recipe:
            raise PermissionError("installation recipe is not trusted")
        if os.geteuid() == 0:
            raise PermissionError("the agent runtime must not run permanently as root")
        executable = Path(recipe.command[0]).resolve(strict=True)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise RuntimeError("trusted package manager is unavailable")
        command = list(recipe.command)
        if recipe.requires_elevation:
            sudo = Path("/usr/bin/sudo")
            if not sudo.is_file():
                raise RuntimeError("non-interactive privilege broker is unavailable")
            command = [str(sudo), "-n", *command]
        try:
            completed = subprocess.run(  # nosec B603
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                shell=False,
                check=False,
                timeout=300.0,
            )
        except subprocess.TimeoutExpired:
            return 124
        return completed.returncode


def _installation_recipe(name: str) -> InstallationRecipe | None:
    packages = _TRUSTED_CATALOG.get(name)
    if packages is None:
        return None
    managers = (
        ("apt", Path("/usr/bin/apt-get"), ("install", "-y"), True),
        ("dnf", Path("/usr/bin/dnf"), ("install", "-y"), True),
        (
            "brew",
            Path("/home/linuxbrew/.linuxbrew/bin/brew"),
            ("install",),
            False,
        ),
    )
    for manager, executable, arguments, elevation in managers:
        package = packages.get(manager)
        if package is not None and executable.is_file():
            return InstallationRecipe(
                tool=name,
                manager=manager,
                package=package,
                command=(str(executable), *arguments, package),
                requires_elevation=elevation,
            )
    return None


def _find_executable(name: str, project_root: Path) -> Path | None:
    candidates = (
        project_root / ".venv" / "bin" / name,
        Path("/usr/bin") / name,
        Path("/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/home/linuxbrew/.linuxbrew/bin") / name,
    )
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
            metadata = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode) and os.access(path, os.X_OK):
            return path
    return None


def _version(path: Path) -> str | None:
    try:
        result = subprocess.run(  # nosec B603
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0][:256] if output else None


def _sandboxed_version(path: Path, project_root: Path) -> str | None:
    """Probe a repository-local executable without giving it host access."""
    try:
        result = subprocess.run(  # nosec B603
            sandbox_command(
                project_root,
                project_root,
                path,
                ["--version"],
            ),
            cwd=project_root,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0][:256] if output else None


def _valid_name(name: object) -> bool:
    return (
        type(name) is str
        and bool(name)
        and len(name) <= 64
        and all(character.isalnum() or character in "._+-" for character in name)
    )


__all__ = [
    "Capability",
    "CapabilityRegistry",
    "InstallResult",
    "InstallationRecipe",
    "PrivilegedSystemExecutor",
]
