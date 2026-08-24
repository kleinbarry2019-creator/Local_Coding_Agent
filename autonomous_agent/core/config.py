"""Trusted project, configuration, and state-path resolution."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

_APP_DIRECTORY = "local-coding-agent"
_PROJECT_CONFIG_NAME = ".local-agent.toml"
_MAX_CONFIG_BYTES = 1_048_576


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


class ExecutionMode(str, Enum):
    MONITORED = "monitored"
    AUTONOMOUS = "autonomous"
    UNRESTRICTED_ROOT = "unrestricted-root"


class ConfigSource(str, Enum):
    BUILTIN = "builtin"
    GLOBAL = "global"
    PROJECT = "project"
    ENVIRONMENT = "environment"
    CLI = "cli"


@dataclass(frozen=True)
class FieldProvenance:
    field: str
    source: ConfigSource
    source_path: str | None


@dataclass(frozen=True)
class ResourceLimits:
    command_timeout_s: float = 10.0
    hard_command_timeout_s: float = 120.0
    max_output_bytes: int = 65_536
    hard_max_output_bytes: int = 1_048_576
    doctor_probe_timeout_s: float = 5.0
    max_cpu_percent: float = 90.0
    min_free_ram_mib: int = 2_048
    max_vram_percent: float = 90.0
    min_free_disk_mib: int = 2_048


@dataclass(frozen=True)
class CliOverrides:
    project_root: Path | None = None
    state_root: Path | None = None
    mode: ExecutionMode | None = None
    command_timeout_s: float | None = None
    max_output_bytes: int | None = None


@dataclass(frozen=True)
class AgentConfig:
    schema_version: int
    mode: ExecutionMode
    paths: ResolvedPaths
    limits: ResourceLimits
    free_only: bool
    audit_required: bool
    provenance: Mapping[str, FieldProvenance]

    def redacted_dict(self) -> dict[str, object]:
        """Return the declared configuration surface with secrets removed."""
        document: dict[str, object] = {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "paths": {
                "config_file": str(self.paths.config_file),
                "project_file": str(self.paths.project_file),
                "project_root": str(self.paths.project_root),
                "state_root": str(self.paths.state_root),
            },
            "limits": {
                item.name: getattr(self.limits, item.name)
                for item in fields(ResourceLimits)
            },
            "free_only": self.free_only,
            "audit_required": self.audit_required,
            "provenance": {
                name: {
                    "field": item.field,
                    "source": item.source.value,
                    "source_path": item.source_path,
                }
                for name, item in self.provenance.items()
            },
        }
        sensitive_values = getattr(self, "_sensitive_values", ())
        return cast(dict[str, object], _redact(document, sensitive_values))


_CORE_FIELDS = frozenset(
    {"mode", "project_root", "state_dir", "free_only", "audit_required"}
)
_LIMIT_FIELDS = frozenset(item.name for item in fields(ResourceLimits))
_SOFT_LIMIT_FIELDS = _LIMIT_FIELDS - {
    "hard_command_timeout_s",
    "hard_max_output_bytes",
}
_SAFER_WHEN_HIGHER = frozenset({"min_free_ram_mib", "min_free_disk_mib"})

# This is deliberately independent of the TOML structure. A known field can
# still be rejected when its source has no authority to set it.
_FIELD_AUTHORITY: Mapping[str, frozenset[ConfigSource]] = MappingProxyType(
    {
        "schema_version": frozenset({ConfigSource.GLOBAL, ConfigSource.PROJECT}),
        "mode": frozenset({ConfigSource.CLI}),
        "project_root": frozenset({ConfigSource.CLI}),
        "state_dir": frozenset({ConfigSource.GLOBAL, ConfigSource.CLI}),
        "free_only": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.ENVIRONMENT}
        ),
        "audit_required": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}
        ),
        "command_timeout_s": frozenset(
            {
                ConfigSource.GLOBAL,
                ConfigSource.PROJECT,
                ConfigSource.ENVIRONMENT,
                ConfigSource.CLI,
            }
        ),
        "hard_command_timeout_s": frozenset(),
        "max_output_bytes": frozenset(
            {
                ConfigSource.GLOBAL,
                ConfigSource.PROJECT,
                ConfigSource.ENVIRONMENT,
                ConfigSource.CLI,
            }
        ),
        "hard_max_output_bytes": frozenset(),
        "doctor_probe_timeout_s": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}
        ),
        "max_cpu_percent": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}
        ),
        "min_free_ram_mib": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}
        ),
        "max_vram_percent": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}
        ),
        "min_free_disk_mib": frozenset(
            {ConfigSource.GLOBAL, ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}
        ),
    }
)

_ENVIRONMENT_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "LOCAL_AGENT_FREE_ONLY": "free_only",
        "LOCAL_AGENT_AUDIT_REQUIRED": "audit_required",
        "LOCAL_AGENT_COMMAND_TIMEOUT_S": "command_timeout_s",
        "LOCAL_AGENT_MAX_OUTPUT_BYTES": "max_output_bytes",
        "LOCAL_AGENT_DOCTOR_PROBE_TIMEOUT_S": "doctor_probe_timeout_s",
        "LOCAL_AGENT_MAX_CPU_PERCENT": "max_cpu_percent",
        "LOCAL_AGENT_MIN_FREE_RAM_MIB": "min_free_ram_mib",
        "LOCAL_AGENT_MAX_VRAM_PERCENT": "max_vram_percent",
        "LOCAL_AGENT_MIN_FREE_DISK_MIB": "min_free_disk_mib",
    }
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def load_config(
    *,
    cwd: Path,
    home: Path,
    environ: Mapping[str, str],
    cli: CliOverrides = CliOverrides(),  # noqa: B008 - immutable value, required API
) -> AgentConfig:
    """Load validated configuration according to explicit source authority."""
    sensitive_values = _collect_sensitive_values(environ)
    try:
        return _load_config(
            cwd=cwd,
            home=home,
            environ=environ,
            cli=cli,
            sensitive_values=sensitive_values,
        )
    except ConfigError as error:
        safe_error = ConfigError(
            error.code,
            error.field,
            _redact_string(error.message, sensitive_values),
        )
    # Raise outside the handler so parser/conversion/path exceptions cannot
    # survive as context or cause in user-visible traceback chains.
    raise safe_error


def _load_config(
    *,
    cwd: Path,
    home: Path,
    environ: Mapping[str, str],
    cli: CliOverrides,
    sensitive_values: tuple[str, ...],
) -> AgentConfig:
    _validate_cli_path_types(cli)
    canonical_home = _validated_absolute_path(home, "home")
    config_file = _global_config_file(canonical_home, environ)
    _validate_global_config(config_file)
    global_document = _read_toml(
        config_file,
        ConfigSource.GLOBAL,
        require_owner_control=True,
    )
    global_entries = _document_entries(global_document, ConfigSource.GLOBAL)

    default_limits = ResourceLimits()
    values: dict[str, object] = {
        "schema_version": 1,
        "mode": ExecutionMode.MONITORED,
        "free_only": True,
        "audit_required": True,
        **{
            item.name: getattr(default_limits, item.name)
            for item in fields(ResourceLimits)
        },
    }
    provenance: dict[str, FieldProvenance] = {
        name: FieldProvenance(name, ConfigSource.BUILTIN, None) for name in values
    }
    _apply_entries(
        values,
        provenance,
        global_entries,
        source=ConfigSource.GLOBAL,
        source_path=str(config_file),
    )

    # Owner-controlled global state is extracted before resolution so it can
    # take its designed precedence over the untrusted XDG platform base.
    trusted_paths = resolve_paths(
        cwd=cwd,
        home=canonical_home,
        environ=environ,
        explicit_project_root=cli.project_root,
        global_state_override=(
            _required_path(values, "state_dir") if "state_dir" in values else None
        ),
        cli_state_override=cli.state_root,
    )
    values["project_root"] = trusted_paths.project_root
    provenance["project_root"] = FieldProvenance(
        "project_root",
        ConfigSource.CLI if cli.project_root is not None else ConfigSource.BUILTIN,
        None,
    )
    if cli.state_root is not None:
        values["state_dir"] = trusted_paths.state_root
        provenance["state_dir"] = FieldProvenance(
            "state_dir", ConfigSource.CLI, None
        )
    elif "state_dir" not in values:
        values["state_dir"] = trusted_paths.state_root
        provenance["state_dir"] = FieldProvenance(
            "state_dir",
            (
                ConfigSource.ENVIRONMENT
                if "XDG_STATE_HOME" in environ
                else ConfigSource.BUILTIN
            ),
            None,
        )

    # The project file is not consulted until the trusted root has been fixed.
    project_document = _read_toml(
        trusted_paths.project_file,
        ConfigSource.PROJECT,
        require_owner_control=False,
    )
    project_entries = _document_entries(project_document, ConfigSource.PROJECT)
    environment_entries = _environment_entries(environ)
    cli_entries = _cli_entries(cli)
    _apply_entries(
        values,
        provenance,
        project_entries,
        source=ConfigSource.PROJECT,
        source_path=str(trusted_paths.project_file),
    )
    _apply_entries(
        values,
        provenance,
        environment_entries,
        source=ConfigSource.ENVIRONMENT,
        source_path=None,
    )
    _apply_entries(
        values,
        provenance,
        cli_entries,
        source=ConfigSource.CLI,
        source_path=None,
    )

    final_paths = resolve_paths(
        cwd=cwd,
        home=canonical_home,
        environ=environ,
        explicit_project_root=_required_path(values, "project_root"),
        global_state_override=(
            _required_path(values, "state_dir")
            if provenance["state_dir"].source is ConfigSource.GLOBAL
            else None
        ),
        cli_state_override=(
            _required_path(values, "state_dir")
            if provenance["state_dir"].source is ConfigSource.CLI
            else None
        ),
    )
    config = AgentConfig(
        schema_version=_required_int(values, "schema_version"),
        mode=_required_mode(values, "mode"),
        paths=final_paths,
        limits=_resource_limits(values),
        free_only=_required_bool(values, "free_only"),
        audit_required=_required_bool(values, "audit_required"),
        provenance=MappingProxyType(dict(provenance)),
    )
    object.__setattr__(config, "_sensitive_values", sensitive_values)
    return config


def _read_toml(
    path: Path,
    source: ConfigSource,
    *,
    require_owner_control: bool,
) -> Mapping[str, object]:
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    except AttributeError as error:
        raise ConfigError(
            "unsupported_platform", "config_file", "requires no-follow file access"
        ) from error
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigError(
            "unsafe_config_file",
            "config_file" if source is ConfigSource.GLOBAL else "project_file",
            "could not be opened safely",
        ) from error

    try:
        metadata = os.fstat(descriptor)
        field = "config_file" if source is ConfigSource.GLOBAL else "project_file"
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError(
                "unsafe_config_file", field, "must be a regular non-symlink file"
            )
        if metadata.st_size > _MAX_CONFIG_BYTES:
            raise ConfigError(
                "config_too_large",
                field,
                "must not exceed the configuration byte limit",
            )
        if require_owner_control and (
            metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ConfigError(
                "unsafe_config_file", field, "must be controlled by the current user"
            )
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                document = tomllib.load(stream)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise ConfigError(
                "invalid_config", field, "contains invalid TOML"
            ) from error
    finally:
        os.close(descriptor)
    return document


def _document_entries(
    document: Mapping[str, object], source: ConfigSource
) -> dict[str, object]:
    entries: dict[str, object] = {}
    for name, value in document.items():
        if name == "schema_version":
            entries[name] = value
            continue
        if name not in {"core", "limits"}:
            raise ConfigError("unknown_field", name, "is not a declared field")
        if not isinstance(value, dict):
            raise ConfigError("invalid_type", name, "must be a TOML table")
        allowed = _CORE_FIELDS if name == "core" else _LIMIT_FIELDS
        for field_name, field_value in value.items():
            if field_name not in allowed:
                raise ConfigError(
                    "unknown_field", field_name, "is not a declared field"
                )
            entries[field_name] = field_value

    _validate_authority(entries, source)
    return entries


def _environment_entries(environ: Mapping[str, str]) -> dict[str, object]:
    for name in environ:
        if name.startswith("LOCAL_AGENT_") and name not in _ENVIRONMENT_FIELDS:
            raise ConfigError("unknown_field", name, "is not a mapped environment field")
    return {
        field_name: environ[environment_name]
        for environment_name, field_name in _ENVIRONMENT_FIELDS.items()
        if environment_name in environ
    }


def _cli_entries(cli: CliOverrides) -> dict[str, object]:
    entries: dict[str, object] = {}
    if cli.project_root is not None:
        entries["project_root"] = cli.project_root
    if cli.state_root is not None:
        entries["state_dir"] = cli.state_root
    if cli.mode is not None:
        entries["mode"] = cli.mode
    if cli.command_timeout_s is not None:
        entries["command_timeout_s"] = cli.command_timeout_s
    if cli.max_output_bytes is not None:
        entries["max_output_bytes"] = cli.max_output_bytes
    return entries


def _validate_cli_path_types(cli: CliOverrides) -> None:
    if cli.project_root is not None and not isinstance(cli.project_root, Path):
        raise ConfigError("invalid_type", "project_root", "must be a path")
    if cli.state_root is not None and not isinstance(cli.state_root, Path):
        raise ConfigError("invalid_type", "state_root", "must be a path")


def _validate_authority(entries: Mapping[str, object], source: ConfigSource) -> None:
    for field_name in entries:
        if source not in _FIELD_AUTHORITY[field_name]:
            raise ConfigError(
                "unauthorized_override",
                field_name,
                f"cannot be set by {source.value} configuration",
            )


def _apply_entries(
    values: dict[str, object],
    provenance: dict[str, FieldProvenance],
    entries: Mapping[str, object],
    *,
    source: ConfigSource,
    source_path: str | None,
) -> None:
    _validate_authority(entries, source)
    for field_name, raw_value in entries.items():
        value = _coerce_value(field_name, raw_value, source)
        _validate_value(field_name, value)
        if source in {ConfigSource.PROJECT, ConfigSource.ENVIRONMENT}:
            _validate_safer_bound(field_name, value, values[field_name], source)
        values[field_name] = value
        provenance[field_name] = FieldProvenance(field_name, source, source_path)


def _coerce_value(
    field_name: str, raw_value: object, source: ConfigSource
) -> object:
    from_environment = source is ConfigSource.ENVIRONMENT
    if field_name in {"schema_version", "max_output_bytes", "min_free_ram_mib", "min_free_disk_mib"}:
        if from_environment:
            return _parse_environment_int(field_name, raw_value)
        if type(raw_value) is not int:
            raise ConfigError("invalid_type", field_name, "must be an integer")
        return raw_value
    if field_name in {
        "command_timeout_s",
        "doctor_probe_timeout_s",
        "max_cpu_percent",
        "max_vram_percent",
    }:
        if from_environment:
            return _parse_environment_float(field_name, raw_value)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ConfigError("invalid_type", field_name, "must be a number")
        return float(raw_value)
    if field_name in {"free_only", "audit_required"}:
        if from_environment:
            return _parse_environment_bool(field_name, raw_value)
        if type(raw_value) is not bool:
            raise ConfigError("invalid_type", field_name, "must be a boolean")
        return raw_value
    if field_name in {"project_root", "state_dir"}:
        if source is ConfigSource.CLI:
            if not isinstance(raw_value, Path):
                raise ConfigError("invalid_type", field_name, "must be a path")
            return raw_value
        if type(raw_value) is not str:
            raise ConfigError("invalid_type", field_name, "must be a path string")
        return Path(raw_value)
    if field_name == "mode":
        if not isinstance(raw_value, ExecutionMode):
            raise ConfigError("invalid_type", field_name, "must be an execution mode")
        return raw_value
    raise ConfigError("unknown_field", field_name, "is not a declared field")


def _parse_environment_int(field_name: str, raw_value: object) -> int:
    if type(raw_value) is str and raw_value and raw_value.strip() == raw_value:
        try:
            return int(raw_value, 10)
        except ValueError:
            pass
    raise ConfigError(
        "invalid_type", field_name, "environment value must be an integer"
    )


def _parse_environment_float(field_name: str, raw_value: object) -> float:
    if type(raw_value) is str and raw_value and raw_value.strip() == raw_value:
        try:
            return float(raw_value)
        except ValueError:
            pass
    raise ConfigError(
        "invalid_type", field_name, "environment value must be a number"
    )


def _parse_environment_bool(field_name: str, raw_value: object) -> bool:
    if raw_value == "true":
        return True
    if raw_value == "false":
        return False
    raise ConfigError(
        "invalid_type", field_name, "environment value must be true or false"
    )


def _validate_value(field_name: str, value: object) -> None:
    if field_name == "schema_version" and value != 1:
        raise ConfigError("unsupported_schema", field_name, "must be version 1")
    if field_name in {"free_only", "audit_required"} and value is not True:
        raise ConfigError("invariant_violation", field_name, "must remain enabled")
    if field_name == "mode" and value is ExecutionMode.UNRESTRICTED_ROOT:
        raise ConfigError(
            "authority_unavailable", field_name, "requires a separate authority grant"
        )
    if field_name in {"command_timeout_s", "doctor_probe_timeout_s"}:
        number = _number(value, field_name)
        if not 0.0 < number <= ResourceLimits().hard_command_timeout_s:
            raise ConfigError(
                "invalid_limit", field_name, "must be positive and within the hard maximum"
            )
    if field_name == "max_output_bytes":
        number = _integer(value, field_name)
        if not 0 < number <= ResourceLimits().hard_max_output_bytes:
            raise ConfigError(
                "invalid_limit", field_name, "must be positive and within the hard maximum"
            )
    if field_name in {"max_cpu_percent", "max_vram_percent"}:
        number = _number(value, field_name)
        if not 0.0 < number <= 100.0:
            raise ConfigError(
                "invalid_limit", field_name, "must be greater than zero and at most 100"
            )
    if (
        field_name in {"min_free_ram_mib", "min_free_disk_mib"}
        and _integer(value, field_name) < 0
    ):
        raise ConfigError("invalid_limit", field_name, "must not be negative")


def _validate_safer_bound(
    field_name: str,
    value: object,
    previous: object,
    source: ConfigSource,
) -> None:
    if field_name not in _SOFT_LIMIT_FIELDS:
        return
    candidate = _number(value, field_name)
    current = _number(previous, field_name)
    safer = (
        candidate >= current
        if field_name in _SAFER_WHEN_HIGHER
        else candidate <= current
    )
    if not safer:
        raise ConfigError(
            "unsafe_widening",
            field_name,
            f"{source.value} configuration may only move toward the safer bound",
        )


def _resource_limits(values: Mapping[str, object]) -> ResourceLimits:
    return ResourceLimits(
        command_timeout_s=_required_float(values, "command_timeout_s"),
        hard_command_timeout_s=_required_float(values, "hard_command_timeout_s"),
        max_output_bytes=_required_int(values, "max_output_bytes"),
        hard_max_output_bytes=_required_int(values, "hard_max_output_bytes"),
        doctor_probe_timeout_s=_required_float(values, "doctor_probe_timeout_s"),
        max_cpu_percent=_required_float(values, "max_cpu_percent"),
        min_free_ram_mib=_required_int(values, "min_free_ram_mib"),
        max_vram_percent=_required_float(values, "max_vram_percent"),
        min_free_disk_mib=_required_int(values, "min_free_disk_mib"),
    )


def _required_int(values: Mapping[str, object], field_name: str) -> int:
    return _integer(values[field_name], field_name)


def _required_float(values: Mapping[str, object], field_name: str) -> float:
    return _number(values[field_name], field_name)


def _required_bool(values: Mapping[str, object], field_name: str) -> bool:
    value = values[field_name]
    if type(value) is not bool:
        raise ConfigError("invalid_type", field_name, "must be a boolean")
    return value


def _required_path(values: Mapping[str, object], field_name: str) -> Path:
    value = values[field_name]
    if not isinstance(value, Path):
        raise ConfigError("invalid_type", field_name, "must be a path")
    return value


def _required_mode(values: Mapping[str, object], field_name: str) -> ExecutionMode:
    value = values[field_name]
    if not isinstance(value, ExecutionMode):
        raise ConfigError("invalid_type", field_name, "must be an execution mode")
    return value


def _integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise ConfigError("invalid_type", field_name, "must be an integer")
    return value


def _number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("invalid_type", field_name, "must be a number")
    return float(value)


def _collect_sensitive_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    values = {
        value
        for name, value in environ.items()
        if value and _sensitive_key(name)
    }
    return tuple(sorted(values, key=len, reverse=True))


def _sensitive_key(name: object) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", separated)
    normalized = "_".join(
        part
        for part in "".join(
            character.lower() if character.isalnum() else "_"
            for character in separated
        ).split("_")
        if part
    )
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact(value: object, sensitive_values: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _sensitive_key(key)
                else _redact(item, sensitive_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, sensitive_values) for item in value)
    if isinstance(value, str):
        return _redact_string(value, sensitive_values)
    return value


def _redact_string(value: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = value
    for sensitive_value in sensitive_values:
        redacted = redacted.replace(sensitive_value, "[REDACTED]")
    return redacted


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


def ensure_state_root(config: AgentConfig) -> Path:
    """Create the already-resolved state root through the secure path walker."""
    if type(config) is not AgentConfig:
        raise TypeError("config must be AgentConfig")
    validate_state_root_isolated(
        config.paths.project_root,
        config.paths.state_root,
    )
    _create_state_path(config.paths.state_root)
    return config.paths.state_root


def validate_state_root_isolated(project_root: Path, state_root: Path) -> None:
    """Keep persistent state outside the writable project process boundary."""
    project = project_root.resolve(strict=True)
    state = state_root.resolve(strict=False)
    if state == project or state.is_relative_to(project):
        raise ConfigError(
            "unsafe_path",
            "state_root",
            "must be outside project_root so project processes cannot modify runtime state",
        )


def _global_config_file(home: Path, environ: Mapping[str, str]) -> Path:
    config_home = environ.get("XDG_CONFIG_HOME")
    if config_home is None:
        config_base = home / ".config"
    else:
        config_base = _validated_xdg_base(
            Path(config_home), home, "XDG_CONFIG_HOME"
        )
    return config_base / _APP_DIRECTORY / "config.toml"


def _validate_global_config(config_file: Path) -> None:
    _validate_trusted_directory_chain(config_file.parent, "config_file")
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
    state_base = _validated_xdg_base(Path(state_home), home, "XDG_STATE_HOME")
    return state_base / _APP_DIRECTORY


def _validated_xdg_base(path: Path, home: Path, field: str) -> Path:
    base = _validated_absolute_path(path, field)
    if base != home and not base.is_relative_to(home):
        raise ConfigError(
            "unsafe_path", field, "must remain beneath the canonical home"
        )
    return base


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
    _validate_trusted_directory_chain(state_root, "state_root")


def _validate_trusted_directory_chain(path: Path, field: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ConfigError("invalid_path", field, str(error)) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfigError("unsafe_path", field, "must not contain symlinks")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError(
                "unsafe_path", field, "must not contain non-directory components"
            )
        owner_is_trusted = metadata.st_uid in {0, os.getuid()}
        writable_by_others = bool(
            metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
        protected_shared_root = bool(
            metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        )
        if not owner_is_trusted or (
            writable_by_others and not protected_shared_root
        ):
            raise ConfigError(
                "unsafe_path",
                field,
                "must not contain an untrusted writable ancestor",
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
