from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from autonomous_agent.core.autonomy import (
    AutonomyRuntime,
    FailureAnalysis,
    FailureCategory,
    RepairAction,
    Replanner,
)
from autonomous_agent.core.config import (
    AgentConfig,
    ConfigError,
    ExecutionMode,
    ResolvedPaths,
    ResourceLimits,
)


def _config(tmp_path: Path) -> AgentConfig:
    project = tmp_path / "project"
    project.mkdir()
    return AgentConfig(
        schema_version=1,
        mode=ExecutionMode.AUTONOMOUS,
        paths=ResolvedPaths(
            config_file=tmp_path / "config.toml",
            project_file=project / ".local-agent.toml",
            project_root=project,
            state_root=tmp_path / "state",
        ),
        limits=ResourceLimits(min_free_ram_mib=1, min_free_disk_mib=1),
        free_only=True,
        audit_required=True,
        provenance=MappingProxyType({}),
    )


def test_runtime_completes_only_after_independent_file_readback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = AutonomyRuntime(config)

    result = runtime.run("Erstelle `result.txt` mit dem Inhalt `verified`")

    assert result.status == "completed"
    assert result.completion.completed
    assert result.completion.executed
    assert result.completion.tested
    assert result.completion.e2e_verified
    assert (config.paths.project_root / "result.txt").read_text() == "verified"
    persisted = runtime.tasks.load_task(result.session_id)
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.completion is not None
    assert persisted.completion["completed"] is True
    assert runtime.audit.verify().ok


def test_runtime_exposes_explicit_undo_for_last_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = AutonomyRuntime(config)
    result = runtime.run("Erstelle `undo-me.txt` mit dem Inhalt `temporary`")
    target = config.paths.project_root / "undo-me.txt"
    assert target.exists()

    undone = runtime.undo(result.session_id)

    assert undone["restored"] is True
    assert undone["audit_ok"] is True
    assert not target.exists()
    assert runtime.audit_status()["ok"] is True


def test_runtime_exports_redacted_audit_log_to_project_protocols(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = AutonomyRuntime(config)
    result = runtime.run("Erstelle `export-me.txt` mit dem Inhalt `logged`")

    exported = runtime.export_audit_log(result.session_id)
    path = config.paths.project_root / str(exported["path"])

    assert exported["event_count"] > 0
    assert path.parent.name == "Protokolle"
    assert path.is_file()
    assert result.session_id in path.read_text(encoding="utf-8")


def test_runtime_rejects_state_inside_project(tmp_path: Path) -> None:
    config = _config(tmp_path)
    unsafe = tmp_path / "project" / ".state"
    unsafe_config = AgentConfig(
        schema_version=config.schema_version,
        mode=config.mode,
        paths=ResolvedPaths(
            config_file=config.paths.config_file,
            project_file=config.paths.project_file,
            project_root=config.paths.project_root,
            state_root=unsafe,
        ),
        limits=config.limits,
        free_only=config.free_only,
        audit_required=config.audit_required,
        provenance=config.provenance,
    )

    with pytest.raises(ConfigError, match="state_root"):
        AutonomyRuntime(unsafe_config)
    assert not unsafe.exists()


def test_exit_zero_is_required_but_not_sufficient_without_e2e(tmp_path: Path) -> None:
    runtime = AutonomyRuntime(_config(tmp_path))

    result = runtime.run("Run python3 -c 'raise SystemExit(7)'")

    assert result.status == "failed"
    assert not result.completion.completed
    assert not result.completion.e2e_verified
    assert any(output.get("success") is False for output in result.outputs)


def test_real_sandbox_command_is_e2e_verified(tmp_path: Path) -> None:
    runtime = AutonomyRuntime(_config(tmp_path))

    result = runtime.run("Run python3 -c 'print(\"RUNTIME_E2E_OK\")'")

    assert result.status == "completed"
    process_output = result.outputs[-1]["data"]
    assert isinstance(process_output, dict)
    assert process_output["stdout"] == "RUNTIME_E2E_OK\n"
    assert result.completion.e2e_verified


def test_completed_task_can_be_recovered_after_runtime_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first_runtime = AutonomyRuntime(config)
    first = first_runtime.run("Write `resume.txt` with content durable")

    restarted = AutonomyRuntime(config)
    resumed = restarted.resume(first.session_id)

    assert resumed.status == "completed"
    assert resumed.completion.completed
    assert resumed.outputs == ()
    assert restarted.audit.verify().ok


def test_replanner_stops_repeated_failures_instead_of_looping() -> None:
    analysis = FailureAnalysis(
        category=FailureCategory.TIMEOUT,
        root_cause="bounded tool deadline expired",
        retryable=True,
    )
    assert (
        Replanner().decide(analysis, repeated=True, mutation_started=True)
        is RepairAction.STOP
    )


def test_runtime_resumes_persisted_running_task_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    runtime = AutonomyRuntime(config)

    def crash(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "_execute_step", crash)
    with pytest.raises(KeyboardInterrupt):
        runtime.run("Write `after-crash.txt` with content RECOVERED")
    with runtime.store.connection() as connection:
        session_id = str(connection.execute("SELECT session_id FROM tasks").fetchone()[0])

    restarted = AutonomyRuntime(config)
    result = restarted.resume(session_id)

    assert result.status == "completed"
    assert (config.paths.project_root / "after-crash.txt").read_text() == "RECOVERED"
    assert restarted.audit.verify().ok


def test_project_checkpoint_rolls_back_process_style_tree_mutations(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runtime = AutonomyRuntime(config)
    completed = runtime.run("Write `original.txt` with content BEFORE")
    checkpoint = runtime.checkpoints.create(
        completed.session_id, "step-tree", config.paths.project_root
    )
    original = config.paths.project_root / "original.txt"
    created = config.paths.project_root / "created.txt"
    original.write_text("AFTER", encoding="utf-8")
    created.write_text("NEW", encoding="utf-8")

    rollback = runtime.checkpoints.restore(checkpoint)

    assert rollback.restored
    assert original.read_text() == "BEFORE"
    assert not created.exists()
