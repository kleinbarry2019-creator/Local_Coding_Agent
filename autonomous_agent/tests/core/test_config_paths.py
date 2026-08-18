from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from autonomous_agent.core.config import ConfigError, resolve_paths


def test_git_root_is_resolved_before_project_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)

    paths = resolve_paths(cwd=nested, home=tmp_path, environ={})

    assert paths.project_root == repo.resolve()
    assert paths.project_file == repo / ".local-agent.toml"


def test_relative_xdg_state_home_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="XDG_STATE_HOME"):
        resolve_paths(
            cwd=tmp_path,
            home=tmp_path,
            environ={"XDG_STATE_HOME": "relative/state"},
        )


def test_explicit_project_root_takes_precedence_over_git_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src"
    nested.mkdir()
    explicit_root = tmp_path / "explicit"
    explicit_root.mkdir()

    paths = resolve_paths(
        cwd=nested,
        home=tmp_path,
        environ={},
        explicit_project_root=explicit_root,
    )

    assert paths.project_root == explicit_root.resolve()
    assert paths.project_file == explicit_root / ".local-agent.toml"


def test_xdg_fallbacks_define_config_and_state_locations(tmp_path: Path) -> None:
    paths = resolve_paths(cwd=tmp_path, home=tmp_path, environ={})

    assert paths.config_file == tmp_path / ".config/local-coding-agent/config.toml"
    assert paths.state_root == tmp_path / ".local/state/local-coding-agent"


def test_xdg_bases_define_config_and_state_locations(tmp_path: Path) -> None:
    config_home = tmp_path / "config-home"
    state_home = tmp_path / "state-home"

    paths = resolve_paths(
        cwd=tmp_path,
        home=tmp_path,
        environ={
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_STATE_HOME": str(state_home),
        },
    )

    assert paths.config_file == config_home / "local-coding-agent/config.toml"
    assert paths.state_root == state_home / "local-coding-agent"


def test_cli_state_override_takes_precedence_over_global_override(
    tmp_path: Path,
) -> None:
    global_root = tmp_path / "global-state"
    cli_root = tmp_path / "cli-state"

    paths = resolve_paths(
        cwd=tmp_path,
        home=tmp_path,
        environ={},
        global_state_override=global_root,
        cli_state_override=cli_root,
    )

    assert paths.state_root == cli_root


def test_global_state_override_precedes_xdg_default(tmp_path: Path) -> None:
    global_root = tmp_path / "global-state"

    paths = resolve_paths(
        cwd=tmp_path,
        home=tmp_path,
        environ={"XDG_STATE_HOME": str(tmp_path / "xdg-state")},
        global_state_override=global_root,
    )

    assert paths.state_root == global_root


def test_state_path_with_symlink_ancestor_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(ConfigError, match="state_root"):
        resolve_paths(
            cwd=tmp_path,
            home=tmp_path,
            environ={"XDG_STATE_HOME": str(linked)},
        )


def test_state_path_with_non_directory_component_fails_closed(tmp_path: Path) -> None:
    component = tmp_path / "not-a-directory"
    component.write_text("file", encoding="utf-8")

    with pytest.raises(ConfigError, match="state_root"):
        resolve_paths(
            cwd=tmp_path,
            home=tmp_path,
            environ={"XDG_STATE_HOME": str(component / "child")},
        )


def test_group_writable_global_config_fails_closed(tmp_path: Path) -> None:
    config_file = tmp_path / ".config/local-coding-agent/config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("", encoding="utf-8")
    config_file.chmod(0o620)

    with pytest.raises(ConfigError, match="config_file"):
        resolve_paths(cwd=tmp_path, home=tmp_path, environ={})


def test_global_config_symlink_fails_closed(tmp_path: Path) -> None:
    config_file = tmp_path / ".config/local-coding-agent/config.toml"
    config_file.parent.mkdir(parents=True)
    target = tmp_path / "config.toml"
    target.write_text("", encoding="utf-8")
    config_file.symlink_to(target)

    with pytest.raises(ConfigError, match="config_file"):
        resolve_paths(cwd=tmp_path, home=tmp_path, environ={})


def test_create_state_false_does_not_mutate_filesystem(tmp_path: Path) -> None:
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    paths = resolve_paths(cwd=tmp_path, home=tmp_path, environ={})

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert paths.state_root == tmp_path / ".local/state/local-coding-agent"
    assert after == before


def test_create_state_creates_owner_only_state_directory(tmp_path: Path) -> None:
    paths = resolve_paths(cwd=tmp_path, home=tmp_path, environ={}, create_state=True)

    state_mode = stat.S_IMODE(paths.state_root.stat().st_mode)
    assert paths.state_root.is_dir()
    assert state_mode == 0o700


def test_existing_global_config_must_be_owned_by_current_user(tmp_path: Path) -> None:
    config_file = tmp_path / ".config/local-coding-agent/config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("", encoding="utf-8")

    assert config_file.stat().st_uid == os.getuid()
    assert resolve_paths(cwd=tmp_path, home=tmp_path, environ={}).config_file == config_file
