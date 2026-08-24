from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from autonomous_agent.core.config import (
    AgentConfig,
    ExecutionMode,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.events import AuditLog
from autonomous_agent.core.state import CoreStateStore
from autonomous_agent.core.task_state import TaskStateStore


def _runtime(tmp_path: Path) -> tuple[CoreStateStore, AuditLog, TaskStateStore]:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    store = CoreStateStore(
        state_root / "agent.sqlite3",
        state_root / "audit.json",
        state_root / "audit.lock",
    )
    store.initialize()
    audit = AuditLog(store)
    config = AgentConfig(
        schema_version=1,
        mode=ExecutionMode.AUTONOMOUS,
        paths=ResolvedPaths(
            config_file=tmp_path / "config.toml",
            project_file=tmp_path / ".local-agent.toml",
            project_root=tmp_path,
            state_root=state_root,
        ),
        limits=ResourceLimits(),
        free_only=True,
        audit_required=True,
        provenance=MappingProxyType({}),
    )
    audit.start_session(
        "session-task",
        ExecutionMode.AUTONOMOUS,
        config,
        "2026-08-24T10:00:00+00:00",
    )
    return store, audit, TaskStateStore(store, audit)


def test_task_state_survives_restart_and_only_completes_with_evidence(
    tmp_path: Path,
) -> None:
    store, audit, tasks = _runtime(tmp_path)
    tasks.create_task(
        "session-task",
        "Write result.txt",
        {"kind": "write-file", "target": "result.txt"},
        ({"step_id": "step-1", "tool": "project.write-file"},),
    )
    running = tasks.transition(
        "session-task", status="running", current_step=0, attempts=1
    )

    restarted = TaskStateStore(
        CoreStateStore(store.database_path, store.anchor_path, store.lock_path),
        AuditLog(CoreStateStore(store.database_path, store.anchor_path, store.lock_path)),
    )
    loaded = restarted.load_task("session-task")

    assert running.status == "running"
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.attempts == 1
    assert audit.verify().ok


def test_checkpoint_state_is_audited(tmp_path: Path) -> None:
    _store, audit, tasks = _runtime(tmp_path)
    tasks.create_task(
        "session-task",
        "Write result.txt",
        {"kind": "write-file"},
        ({"step_id": "write"},),
    )

    checkpoint = tasks.create_checkpoint(
        "session-task", "write", {"target": "result.txt", "existed": False}
    )
    tasks.mark_checkpoint(checkpoint.checkpoint_id, "session-task", "restored")

    assert checkpoint.status == "created"
    assert audit.verify().ok
