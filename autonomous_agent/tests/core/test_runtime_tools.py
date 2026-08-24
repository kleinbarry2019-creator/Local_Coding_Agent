from __future__ import annotations

import time
from pathlib import Path

from autonomous_agent.core.config import ExecutionMode, ResourceLimits
from autonomous_agent.core.policy import PolicyContext, ScopeEvidence
from autonomous_agent.core.runtime_tools import ProjectToolRuntime
from autonomous_agent.core.tools import ExecutionContext, SchemaLimits, ToolStatus


def _context(root: Path, targets: tuple[Path, ...]) -> ExecutionContext:
    return ExecutionContext(
        policy=PolicyContext(
            mode=ExecutionMode.AUTONOMOUS,
            canonical_project_root=root,
            scope=ScopeEvidence(
                project_root=root,
                resolved_targets=targets,
                resolver_id="trusted-path-resolver-v1",
                valid=True,
            ),
            hard_limits=ResourceLimits(),
            authority=None,
            recovery=None,
            doctor_capabilities=frozenset(
                {"doctor.read", "doctor.ollama.loopback"}
            ),
        ),
        deadline_monotonic=time.monotonic() + 30.0,
        schema_limits=SchemaLimits(max_output_bytes=1_048_576),
        session_id="session-test",
    )


def test_shared_registry_writes_and_reads_inside_scope(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    target = root / "result.txt"
    registry = ProjectToolRuntime(root).registry()

    written = registry.execute(
        "project.write-file",
        {"path": str(target), "content": "verified"},
        _context(root, (target,)),
    )
    read = registry.execute(
        "project.read-file",
        {"path": str(target)},
        _context(root, (target,)),
    )

    assert written.status is ToolStatus.OK
    assert read.status is ToolStatus.OK
    assert read.data == {
        "byte_size": 8,
        "content": "verified",
        "path": "result.txt",
    }


def test_shared_registry_creates_missing_parent_directories_safely(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    target = root / "nested" / "deeper" / "result.txt"
    registry = ProjectToolRuntime(root).registry()

    result = registry.execute(
        "project.write-file",
        {"path": str(target), "content": "nested"},
        _context(root, (target,)),
    )

    assert result.status is ToolStatus.OK
    assert target.read_text(encoding="utf-8") == "nested"


def test_shared_registry_denies_scope_mismatch(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    target = root / "result.txt"
    registry = ProjectToolRuntime(root).registry()

    result = registry.execute(
        "project.write-file",
        {"path": str(target), "content": "blocked"},
        _context(root, (root / "different.txt",)),
    )

    assert result.status is ToolStatus.DENIED
    assert result.diagnostic_code == "invalid_scope"
    assert not target.exists()


def test_shared_registry_rejects_symlink_escape(tmp_path: Path) -> None:
    root = (tmp_path / "project").resolve()
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(outside)
    registry = ProjectToolRuntime(root).registry()

    result = registry.execute(
        "project.read-file",
        {"path": str(link)},
        _context(root, (link,)),
    )

    assert result.status is ToolStatus.ERROR
    assert result.diagnostic_code == "internal_error"


def test_process_runs_without_shell_in_networkless_bubblewrap(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    registry = ProjectToolRuntime(root).registry()

    result = registry.execute(
        "project.run-process",
        {"argv": ["python3", "-c", "print('E2E_OK')"], "cwd": str(root)},
        _context(root, (root,)),
    )

    assert result.status is ToolStatus.OK
    assert result.data is not None
    assert result.data["exit_code"] == 0
    assert result.data["stdout"] == "E2E_OK\n"
