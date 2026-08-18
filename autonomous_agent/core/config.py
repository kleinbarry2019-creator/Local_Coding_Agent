"""Trusted project, configuration, and state-path resolution."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_APP_DIRECTORY = "local-coding-agent"
_PROJECT_CONFIG_NAME = ".local-agent.toml"


@dataclass(frozen=True)
class ConfigError(Exception):
    code: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.field}: {self.message}"


@dataclass(frozen=True)
class ResolvedPaths:
    config_file: Path
    project_file: Path
    project_root: Path
    state_root: Path


def resolve_paths(
    *,
    cwd: Path,
    home: Path,
    environ: Mapping[str, str],
    explicit_project_root: Path | None = None,
    global_state_override: Path | None = None,
    cli_state_override: Path | None = None,
    create_state: bool = False,
) -> ResolvedPaths:
    """Resolve trusted locations without reading project configuration."""
    canonical_home = _validated_absolute_path(home, "home")
    config_file = _global_config_file(canonical_home, environ)
    _validate_global_config(config_file)

    project_root = _project_root(cwd, explicit_project_root)
    state_root = _state_root(
        canonical_home,
        environ,
        global_state_override,
        cli_state_override,
    )
    _validate_state_path(state_root)
    if create_state:
        _create_state_path(state_root)

    return ResolvedPaths(
        config_file=config_file,
        project_file=project_root / _PROJECT_CONFIG_NAME,
        project_root=project_root,
        state_root=state_root,
    )


def _global_config_file(home: Path, environ: Mapping[str, str]) -> Path:
    config_home = environ.get("XDG_CONFIG_HOME")
    if config_home is None:
        config_base = home / ".config"
    else:
        config_base = _validated_absolute_path(Path(config_home), "XDG_CONFIG_HOME")
    return config_base / _APP_DIRECTORY / "config.toml"


def _validate_global_config(config_file: Path) -> None:
    try:
        metadata = config_file.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ConfigError("invalid_path", "config_file", str(error)) from error

    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigError(
            "unsafe_config_file", "config_file", "must be a regular non-symlink file"
        )
    if metadata.st_uid != os.getuid():
        raise ConfigError(
            "unsafe_config_file", "config_file", "must be owned by the current user"
        )
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            "unsafe_config_file",
            "config_file",
            "must not be writable by group or other users",
        )


def _project_root(cwd: Path, explicit_project_root: Path | None) -> Path:
    if explicit_project_root is not None:
        return _resolve_existing_directory(explicit_project_root, "explicit_project_root")

    canonical_cwd = _resolve_existing_directory(cwd, "cwd")
    current = canonical_cwd
    while True:
        marker = current / ".git"
        if marker.is_file() or marker.is_dir():
            return current
        if current.parent == current:
            return canonical_cwd
        current = current.parent


def _state_root(
    home: Path,
    environ: Mapping[str, str],
    global_state_override: Path | None,
    cli_state_override: Path | None,
) -> Path:
    if cli_state_override is not None:
        return _validated_absolute_path(cli_state_override, "cli_state_override")
    if global_state_override is not None:
        return _validated_absolute_path(global_state_override, "global_state_override")

    state_home = environ.get("XDG_STATE_HOME")
    if state_home is None:
        return home / ".local" / "state" / _APP_DIRECTORY
    state_base = _validated_absolute_path(Path(state_home), "XDG_STATE_HOME")
    return state_base / _APP_DIRECTORY


def _validated_absolute_path(path: Path, field: str) -> Path:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise ConfigError(
            "invalid_path", field, "must be an absolute normalized path"
        )
    return path


def _resolve_existing_directory(path: Path, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ConfigError("invalid_path", field, "must resolve to an existing directory") from error
    if not resolved.is_dir():
        raise ConfigError("invalid_path", field, "must resolve to an existing directory")
    return resolved


def _validate_state_path(state_root: Path) -> None:
    current = Path(state_root.anchor)
    for component in state_root.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ConfigError("invalid_path", "state_root", str(error)) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfigError("unsafe_path", "state_root", "must not contain symlinks")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError(
                "unsafe_path", "state_root", "must not contain non-directory components"
            )


def _create_state_path(state_root: Path) -> None:
    current_fd = _open_state_directory(state_root.anchor, None)
    try:
        for component in state_root.parts[1:]:
            next_fd = _open_or_create_state_directory(component, current_fd)
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)
    _validate_state_path(state_root)


def _open_or_create_state_directory(component: str, parent_fd: int) -> int:
    try:
        return _open_state_directory(component, parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(component, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise ConfigError("state_creation_failed", "state_root", str(error)) from error
        try:
            return _open_state_directory(component, parent_fd)
        except OSError as error:
            raise ConfigError("unsafe_path", "state_root", str(error)) from error
    except OSError as error:
        raise ConfigError("unsafe_path", "state_root", str(error)) from error


def _open_state_directory(component: str, parent_fd: int | None) -> int:
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as error:
        raise ConfigError(
            "unsupported_platform",
            "state_root",
            "requires no-follow directory descriptors",
        ) from error
    return os.open(component, flags, dir_fd=parent_fd)
