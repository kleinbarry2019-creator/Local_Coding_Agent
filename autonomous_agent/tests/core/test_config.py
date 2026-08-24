from __future__ import annotations

import os
import signal
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

import autonomous_agent.core.config as config_module
from autonomous_agent.core.config import (
    AgentConfig,
    CliOverrides,
    ConfigError,
    ConfigSource,
    ExecutionMode,
    FieldProvenance,
    ResourceLimits,
    load_config,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return root


def _write_global(home: Path, document: str) -> Path:
    config_file = home / ".config/local-coding-agent/config.toml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(document, encoding="utf-8")
    config_file.chmod(0o600)
    return config_file


def test_builtin_defaults_are_safe_and_complete(project: Path) -> None:
    config = load_config(cwd=project, home=project.parent, environ={})

    assert config.schema_version == 1
    assert config.mode is ExecutionMode.MONITORED
    assert config.limits == ResourceLimits()
    assert config.free_only is True
    assert config.audit_required is True
    assert set(config.provenance) == {
        "schema_version",
        "mode",
        "project_root",
        "state_dir",
        "free_only",
        "audit_required",
        "command_timeout_s",
        "hard_command_timeout_s",
        "max_output_bytes",
        "hard_max_output_bytes",
        "doctor_probe_timeout_s",
        "max_cpu_percent",
        "min_free_ram_mib",
        "max_vram_percent",
        "min_free_disk_mib",
    }
    assert all(
        item.source is ConfigSource.BUILTIN for item in config.provenance.values()
    )


def test_later_authorized_sources_override_earlier_sources(project: Path) -> None:
    home = project.parent
    global_file = _write_global(
        home,
        "[limits]\ncommand_timeout_s = 20.0\nmax_output_bytes = 900000\n",
    )
    global_config = load_config(cwd=project, home=home, environ={})
    assert global_config.limits.command_timeout_s == 20.0
    assert global_config.provenance["command_timeout_s"] == FieldProvenance(
        "command_timeout_s", ConfigSource.GLOBAL, str(global_file)
    )

    (project / ".local-agent.toml").write_text(
        "[limits]\ncommand_timeout_s = 18.0\nmax_output_bytes = 800000\n",
        encoding="utf-8",
    )
    project_config = load_config(cwd=project, home=home, environ={})
    assert project_config.limits.command_timeout_s == 18.0
    assert project_config.provenance["command_timeout_s"] == FieldProvenance(
        "command_timeout_s",
        ConfigSource.PROJECT,
        str(project / ".local-agent.toml"),
    )

    environment = {
        "LOCAL_AGENT_COMMAND_TIMEOUT_S": "16.0",
        "LOCAL_AGENT_MAX_OUTPUT_BYTES": "700000",
    }
    environment_config = load_config(cwd=project, home=home, environ=environment)
    assert environment_config.limits.command_timeout_s == 16.0
    assert environment_config.provenance["command_timeout_s"] == FieldProvenance(
        "command_timeout_s", ConfigSource.ENVIRONMENT, None
    )

    cli_config = load_config(
        cwd=project,
        home=home,
        environ=environment,
        cli=CliOverrides(command_timeout_s=14.0, max_output_bytes=600_000),
    )
    assert cli_config.limits.command_timeout_s == 14.0
    assert cli_config.limits.max_output_bytes == 600_000
    assert cli_config.provenance["command_timeout_s"] == FieldProvenance(
        "command_timeout_s", ConfigSource.CLI, None
    )


def test_global_and_cli_state_precedence_uses_trusted_path_inputs(
    project: Path,
) -> None:
    home = project.parent
    global_state = home / "global-state"
    cli_state = home / "cli-state"
    _write_global(home, f'[core]\nstate_dir = "{global_state}"\n')

    global_config = load_config(cwd=project, home=home, environ={})
    cli_config = load_config(
        cwd=project,
        home=home,
        environ={},
        cli=CliOverrides(state_root=cli_state),
    )

    assert global_config.paths.state_root == global_state
    assert global_config.provenance["state_dir"].source is ConfigSource.GLOBAL
    assert cli_config.paths.state_root == cli_state
    assert cli_config.provenance["state_dir"].source is ConfigSource.CLI


def test_global_state_override_precedes_untrusted_xdg_state_base(
    project: Path,
) -> None:
    global_state = project.parent / "global-state"
    _write_global(
        project.parent,
        f'[core]\nstate_dir = "{global_state}"\n',
    )

    config = load_config(
        cwd=project,
        home=project.parent,
        environ={"XDG_STATE_HOME": "relative/untrusted-state"},
    )

    assert config.paths.state_root == global_state
    assert config.provenance["state_dir"].source is ConfigSource.GLOBAL


@pytest.mark.parametrize(
    ("document", "field"),
    [
        ('[core]\nmode = "autonomous"\n', "mode"),
        ('[core]\nproject_root = "/"\n', "project_root"),
        ('[core]\nstate_dir = "/tmp/state"\n', "state_dir"),
        ('[core]\nfree_only = false\n', "free_only"),
        ('[core]\naudit_required = false\n', "audit_required"),
        ('[limits]\nhard_command_timeout_s = 999\n', "hard_command_timeout_s"),
    ],
)
def test_project_security_widening_is_rejected(
    project: Path, document: str, field: str
) -> None:
    (project / ".local-agent.toml").write_text(document, encoding="utf-8")
    with pytest.raises(ConfigError, match=field):
        load_config(cwd=project, home=project.parent, environ={})


def test_project_cannot_select_free_only_even_when_value_is_true(
    project: Path,
) -> None:
    (project / ".local-agent.toml").write_text(
        "[core]\nfree_only = true\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert raised.value.code == "unauthorized_override"
    assert raised.value.field == "free_only"


@pytest.mark.parametrize(
    ("document", "field"),
    [
        ('[core]\nmode = "autonomous"\n', "mode"),
        ('[core]\nproject_root = "/"\n', "project_root"),
        ('[limits]\nhard_max_output_bytes = 999\n', "hard_max_output_bytes"),
    ],
)
def test_global_source_cannot_set_untrusted_authority_fields(
    project: Path, document: str, field: str
) -> None:
    _write_global(project.parent, document)

    with pytest.raises(ConfigError, match=field):
        load_config(cwd=project, home=project.parent, environ={})


def test_project_limits_may_only_move_toward_safer_bounds(project: Path) -> None:
    _write_global(
        project.parent,
        "[limits]\ncommand_timeout_s = 20.0\nmin_free_ram_mib = 1024\n",
    )
    (project / ".local-agent.toml").write_text(
        "[limits]\ncommand_timeout_s = 15.0\nmin_free_ram_mib = 4096\n",
        encoding="utf-8",
    )

    config = load_config(cwd=project, home=project.parent, environ={})

    assert config.limits.command_timeout_s == 15.0
    assert config.limits.min_free_ram_mib == 4096


@pytest.mark.parametrize(
    ("global_document", "project_document", "field"),
    [
        (
            "[limits]\ncommand_timeout_s = 20.0\n",
            "[limits]\ncommand_timeout_s = 21.0\n",
            "command_timeout_s",
        ),
        (
            "[limits]\nmin_free_ram_mib = 4096\n",
            "[limits]\nmin_free_ram_mib = 2048\n",
            "min_free_ram_mib",
        ),
    ],
)
def test_project_limit_widening_is_rejected(
    project: Path, global_document: str, project_document: str, field: str
) -> None:
    _write_global(project.parent, global_document)
    (project / ".local-agent.toml").write_text(
        project_document, encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=field):
        load_config(cwd=project, home=project.parent, environ={})


def test_environment_limit_widening_is_rejected(project: Path) -> None:
    with pytest.raises(ConfigError, match="command_timeout_s"):
        load_config(
            cwd=project,
            home=project.parent,
            environ={"LOCAL_AGENT_COMMAND_TIMEOUT_S": "11.0"},
        )


def test_cli_can_activate_autonomous_mode(project: Path) -> None:
    config = load_config(
        cwd=project,
        home=project.parent,
        environ={},
        cli=CliOverrides(mode=ExecutionMode.AUTONOMOUS),
    )

    assert config.mode is ExecutionMode.AUTONOMOUS
    assert config.provenance["mode"].source is ConfigSource.CLI


def test_cli_cannot_activate_unrestricted_root_mode(project: Path) -> None:
    with pytest.raises(ConfigError, match="mode"):
        load_config(
            cwd=project,
            home=project.parent,
            environ={},
            cli=CliOverrides(mode=ExecutionMode.UNRESTRICTED_ROOT),
        )


@pytest.mark.parametrize("field", ["project_root", "state_root"])
def test_type_confused_cli_paths_raise_field_specific_config_error(
    project: Path, field: str
) -> None:
    confused_path = cast(Path, "not-a-path-object")
    cli = (
        CliOverrides(project_root=confused_path)
        if field == "project_root"
        else CliOverrides(state_root=confused_path)
    )

    with pytest.raises(ConfigError) as raised:
        load_config(
            cwd=project,
            home=project.parent,
            environ={},
            cli=cli,
        )

    assert raised.value.field == field


@pytest.mark.parametrize(
    "document",
    [
        "unexpected = 1\n",
        "[core]\nunexpected = 1\n",
        "[limits]\nunexpected = 1\n",
        "[unexpected]\nvalue = 1\n",
    ],
)
def test_unknown_toml_fields_are_rejected(project: Path, document: str) -> None:
    _write_global(project.parent, document)

    with pytest.raises(ConfigError, match="unexpected"):
        load_config(cwd=project, home=project.parent, environ={})


def test_unknown_local_agent_environment_field_is_rejected(project: Path) -> None:
    with pytest.raises(ConfigError, match="LOCAL_AGENT_MODE"):
        load_config(
            cwd=project,
            home=project.parent,
            environ={"LOCAL_AGENT_MODE": "autonomous"},
        )


def test_local_agent_state_dir_environment_override_is_rejected(
    project: Path,
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(
            cwd=project,
            home=project.parent,
            environ={"LOCAL_AGENT_STATE_DIR": str(project.parent / "state")},
        )

    assert raised.value.code == "unknown_field"
    assert raised.value.field == "LOCAL_AGENT_STATE_DIR"


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "fork"),
    reason="requires POSIX FIFO and process primitives",
)
def test_project_fifo_is_rejected_without_blocking(project: Path) -> None:
    os.mkfifo(project / ".local-agent.toml")
    child_pid = os.fork()
    if child_pid == 0:
        try:
            load_config(cwd=project, home=project.parent, environ={})
        except ConfigError:
            os._exit(0)
        os._exit(3)

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if waited_pid == child_pid:
            assert os.waitstatus_to_exitcode(status) == 0
            return
        time.sleep(0.01)

    os.kill(child_pid, signal.SIGKILL)
    os.waitpid(child_pid, 0)
    pytest.fail("load_config blocked while opening a project FIFO")


def test_project_config_symlink_is_rejected(project: Path) -> None:
    target = project.parent / "untrusted-project-config.toml"
    target.write_text("", encoding="utf-8")
    (project / ".local-agent.toml").symlink_to(target)

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert raised.value.field == "project_file"


def test_oversized_project_config_is_rejected_before_toml_parsing(
    project: Path,
) -> None:
    project_file = project / ".local-agent.toml"
    project_file.write_bytes(b"#" + (b"x" * 1_048_576))

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert raised.value.code == "config_too_large"
    assert raised.value.field == "project_file"


def test_load_config_rejects_group_writable_global_file(project: Path) -> None:
    config_file = _write_global(project.parent, "")
    config_file.chmod(0o620)

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert raised.value.field == "config_file"


def test_load_config_rejects_global_config_symlink(project: Path) -> None:
    target = project.parent / "global-target.toml"
    target.write_text("", encoding="utf-8")
    config_file = project.parent / ".config/local-coding-agent/config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.symlink_to(target)

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert raised.value.field == "config_file"


def test_load_config_rejects_global_file_not_owned_by_current_user(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_global(project.parent, "")
    recorded_uid = os.getuid()
    monkeypatch.setattr(config_module.os, "getuid", lambda: recorded_uid + 1)

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert raised.value.field == "config_file"


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("global", "nan"),
        ("global", "inf"),
        ("global", "-inf"),
        ("environment", "nan"),
        ("environment", "inf"),
        ("environment", "-inf"),
        ("cli", float("nan")),
        ("cli", float("inf")),
        ("cli", float("-inf")),
    ],
)
def test_non_finite_limits_are_rejected(
    project: Path, source: str, value: str | float
) -> None:
    environ: dict[str, str] = {}
    cli = CliOverrides()
    if source == "global":
        _write_global(
            project.parent,
            f"[limits]\ncommand_timeout_s = {value}\n",
        )
    elif source == "environment":
        assert isinstance(value, str)
        environ["LOCAL_AGENT_COMMAND_TIMEOUT_S"] = value
    else:
        assert isinstance(value, float)
        cli = CliOverrides(command_timeout_s=value)

    with pytest.raises(ConfigError) as raised:
        load_config(
            cwd=project,
            home=project.parent,
            environ=environ,
            cli=cli,
        )

    assert raised.value.field == "command_timeout_s"


def test_boolean_is_not_accepted_as_an_integer(project: Path) -> None:
    _write_global(project.parent, "[limits]\nmax_output_bytes = true\n")

    with pytest.raises(ConfigError, match="max_output_bytes"):
        load_config(cwd=project, home=project.parent, environ={})


@pytest.mark.parametrize(
    ("source", "field"),
    [
        ("global", "free_only"),
        ("global", "audit_required"),
        ("project", "free_only"),
        ("project", "audit_required"),
        ("environment", "free_only"),
        ("environment", "audit_required"),
    ],
)
def test_invariants_cannot_be_disabled_from_any_mapped_source(
    project: Path, source: str, field: str
) -> None:
    if source == "global":
        _write_global(project.parent, f"[core]\n{field} = false\n")
        environ: dict[str, str] = {}
    elif source == "project":
        (project / ".local-agent.toml").write_text(
            f"[core]\n{field} = false\n", encoding="utf-8"
        )
        environ = {}
    else:
        environ = {f"LOCAL_AGENT_{field.upper()}": "false"}

    with pytest.raises(ConfigError, match=field):
        load_config(cwd=project, home=project.parent, environ=environ)


@pytest.mark.parametrize(
    ("document", "field"),
    [
        ("schema_version = 2\n", "schema_version"),
        ("[limits]\ncommand_timeout_s = 121.0\n", "command_timeout_s"),
        ("[limits]\nmax_output_bytes = 1048577\n", "max_output_bytes"),
        ("[limits]\nmax_cpu_percent = 0.0\n", "max_cpu_percent"),
    ],
)
def test_global_values_must_respect_fixed_validation_bounds(
    project: Path, document: str, field: str
) -> None:
    _write_global(project.parent, document)

    with pytest.raises(ConfigError, match=field):
        load_config(cwd=project, home=project.parent, environ={})


def test_cli_values_must_respect_immutable_hard_maxima(project: Path) -> None:
    with pytest.raises(ConfigError, match="max_output_bytes"):
        load_config(
            cwd=project,
            home=project.parent,
            environ={},
            cli=CliOverrides(max_output_bytes=1_048_577),
        )


def test_malformed_toml_error_does_not_echo_document_contents(project: Path) -> None:
    secret = "do-not-leak-this-secret"
    _write_global(project.parent, f'[core]\nstate_dir = "{secret}\n')

    with pytest.raises(ConfigError) as raised:
        load_config(cwd=project, home=project.parent, environ={})

    assert secret not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_invalid_environment_value_is_not_exposed_in_error(project: Path) -> None:
    secret = "credential-value-that-must-not-leak"

    with pytest.raises(ConfigError) as raised:
        load_config(
            cwd=project,
            home=project.parent,
            environ={"LOCAL_AGENT_COMMAND_TIMEOUT_S": secret},
        )

    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("environment_name", "raw_value"),
    [
        ("LOCAL_AGENT_MAX_OUTPUT_BYTES", "integer-secret-value"),
        ("LOCAL_AGENT_COMMAND_TIMEOUT_S", "float-secret-value"),
    ],
)
def test_invalid_environment_conversion_leaves_no_raw_exception_chain(
    project: Path, environment_name: str, raw_value: str
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_config(
            cwd=project,
            home=project.parent,
            environ={environment_name: raw_value},
        )

    assert raw_value not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_redacted_output_has_only_declared_fields_and_hides_sensitive_values(
    project: Path,
) -> None:
    secret = str(project)
    config = load_config(
        cwd=project,
        home=project.parent,
        environ={"API_TOKEN": secret},
    )

    output = config.redacted_dict()

    assert set(output) == {
        "schema_version",
        "mode",
        "paths",
        "limits",
        "free_only",
        "audit_required",
        "provenance",
    }
    assert set(output["paths"]) == {
        "config_file",
        "project_file",
        "project_root",
        "state_root",
    }
    assert set(output["limits"]) == set(ResourceLimits.__dataclass_fields__)
    assert secret not in repr(output)


def test_redaction_normalizes_camel_case_sensitive_environment_keys(
    project: Path,
) -> None:
    secret = str(project.resolve())
    config = load_config(
        cwd=project,
        home=project.parent,
        environ={"SERVICE_APIKey": secret},
    )

    assert secret not in repr(config.redacted_dict())


def test_provenance_is_deeply_immutable(project: Path) -> None:
    config = load_config(cwd=project, home=project.parent, environ={})

    with pytest.raises(TypeError):
        config.provenance["mode"] = FieldProvenance(  # type: ignore[index]
            "mode", ConfigSource.CLI, None
        )
    with pytest.raises(FrozenInstanceError):
        config.provenance["mode"].source = ConfigSource.CLI  # type: ignore[misc]


def test_redacted_output_serializes_enums_paths_and_provenance(project: Path) -> None:
    config: AgentConfig = load_config(
        cwd=project,
        home=project.parent,
        environ={},
        cli=CliOverrides(mode=ExecutionMode.AUTONOMOUS),
    )

    output: dict[str, Any] = config.redacted_dict()

    assert output["mode"] == "autonomous"
    assert output["paths"]["project_root"] == str(project.resolve())
    assert output["provenance"]["mode"] == {
        "field": "mode",
        "source": "cli",
        "source_path": None,
    }
