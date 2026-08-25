from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from autonomous_agent.core.autonomy import (
    AutonomyRuntime,
    CompletionEvaluator,
    FailureAnalysis,
    FailureAnalyzer,
    FailureCategory,
    Planner,
    PlanStep,
    RepairAction,
    Replanner,
    StepKind,
)
from autonomous_agent.core.capabilities import CapabilityRegistry
from autonomous_agent.core.config import (
    AgentConfig,
    ConfigError,
    ExecutionMode,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.goals import GoalNormalizer


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
    assert result.plan_assessment is not None
    assert result.plan_assessment["valid"] is True
    assert result.plan_assessment["ordered_step_ids"] == ["step-write"]
    assert runtime.audit.verify().ok


def test_runtime_can_analyze_project_languages_and_test_hints(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.paths.project_root / "pyproject.toml").write_text(
        "[project]\nname='demo'\n", encoding="utf-8"
    )
    (config.paths.project_root / "main.py").write_text(
        "print('ok')\n", encoding="utf-8"
    )
    result = AutonomyRuntime(config).run(
        "Analysiere das Projekt und seine Programmiersprachen"
    )
    assert result.status == "completed"
    assert result.completion.completed
    analysis = result.outputs[-1]["data"]
    assert isinstance(analysis, dict)
    assert analysis["languages"] == {"Python": 1}
    assert result.plan_assessment is not None
    assert result.plan_assessment["valid"] is True


def test_windows_vm_request_runs_bounded_preflight_without_claiming_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "autonomous_agent.core.capabilities.PrivilegedSystemExecutor.install",
        lambda _self, _recipe: 1,
    )
    config = _config(tmp_path)
    result = AutonomyRuntime(config).run(
        "Baue mir eine Windows VM mit Zugriff auf CPU, GPU und Speicher"
    )

    assert result.status == "failed"
    assert result.completion.completed is False
    assert result.completion.original_goal_matched is False
    assert any(
        criterion.criterion_id == "created"
        and criterion.evidence == "vm-creation-not-performed"
        for criterion in result.completion.criteria
    )
    assert any(
        item.get("phase") == "research"
        and item.get("capability") == "qemu-system-x86_64"
        for item in result.problem_solving
    )
    assert result.plan_assessment is not None
    assert result.plan_assessment["ordered_step_ids"] == [
        "step-vm-hypervisor",
        "step-vm-preflight",
    ]


def test_missing_python_module_is_classified_without_unbounded_install_retry(
    tmp_path: Path,
) -> None:
    result = AutonomyRuntime(_config(tmp_path)).run(
        "Run python3 -c 'import module_that_is_not_installed_for_acb_stress'"
    )

    assert result.status == "failed"
    diagnosis = next(
        item for item in result.problem_solving if item.get("phase") == "diagnosis"
    )
    assert diagnosis["category"] == "dependency-missing"
    assert diagnosis["missing_dependency"] == "module_that_is_not_installed_for_acb_stress"
    assert any(
        item.get("phase") == "research"
        and item.get("dependency") == "module_that_is_not_installed_for_acb_stress"
        and item.get("browser_opened") is False
        for item in result.problem_solving
    )


def test_unfamiliar_complex_goal_gets_browserless_bounded_research(
    tmp_path: Path,
) -> None:
    result = AutonomyRuntime(_config(tmp_path)).run(
        "Implementiere eine Android-App mit Offline-Synchronisierung"
    )

    assert result.status == "failed"
    assert result.completion.completed is False
    assert any(
        item.get("phase") == "research"
        and item.get("browser_opened") is False
        and item.get("network_used") is False
        for item in result.problem_solving
    )
    assert len(result.outputs) == 1


def test_standalone_research_goal_completes_with_bounded_browserless_evidence(
    tmp_path: Path,
) -> None:
    result = AutonomyRuntime(_config(tmp_path)).run(
        "Recherchiere autonom im Hintergrund nach vertrauenswürdigen Quellen"
    )

    assert result.status == "completed"
    assert result.completion.completed is True
    assert result.completion.e2e_verified is True
    assert any(
        item.get("phase") == "research"
        and item.get("browser_opened") is False
        for item in result.problem_solving
    )


@pytest.mark.parametrize(
    "goal_text",
    [
        "Research trusted sources and write the findings to research.txt",
        "Recherchiere vertrauenswürdige Quellen und speichere die Ergebnisse in findings.md",
    ],
)
def test_research_artifact_goal_never_claims_completion_without_artifact(
    tmp_path: Path, goal_text: str
) -> None:
    result = AutonomyRuntime(_config(tmp_path)).run(goal_text)

    assert result.status == "failed"
    assert result.completion.completed is False
    assert result.completion.e2e_verified is False
    assert not (tmp_path / "project" / "research.txt").exists()
    assert not (tmp_path / "project" / "findings.md").exists()


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
    assert result.problem_solving
    assert any(
        event.get("category") == "command-failed"
        for event in result.problem_solving
    )


def test_completion_uses_latest_verified_observation_after_a_replan(tmp_path: Path) -> None:
    config = _config(tmp_path)
    goal = GoalNormalizer().normalize("Write `replanned.txt` with content stable")
    (config.paths.project_root / "replanned.txt").write_text(
        "stable", encoding="utf-8"
    )
    report = CompletionEvaluator().evaluate(
        goal,
        config.paths.project_root,
        (
            {"step_id": "step-write", "success": False, "status": "error"},
            {
                "step_id": "step-write",
                "success": True,
                "status": "ok",
                "data": {"written": True},
            },
        ),
        CapabilityRegistry(config.paths.project_root),
    )
    assert report.completed


def test_failure_analyzer_extracts_missing_command_and_recovery_options() -> None:
    step = PlanStep(
        "step-process",
        StepKind.TOOL,
        "project.run-process",
        {"argv": ["bash", "-lc", "missing-tool --version"]},
        Path("/tmp/project"),
        True,
    )
    analysis = FailureAnalyzer().analyze(
        step,
        {
            "success": False,
            "status": "error",
            "diagnostic_code": "process_failed",
            "data": {
                "exit_code": 127,
                "stderr": "bash: missing-tool: command not found",
            },
        },
    )
    assert analysis.category is FailureCategory.DEPENDENCY_MISSING
    assert analysis.missing_capability == "missing-tool"
    assert "verify-capability" in analysis.candidate_actions


def test_planner_rejects_dependency_cycles_before_execution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    plan = (
        PlanStep(
            "a", StepKind.TOOL, "project.list-files", {}, project, False, ("b",)
        ),
        PlanStep(
            "b", StepKind.TOOL, "project.list-files", {}, project, False, ("a",)
        ),
    )
    assessment = Planner().assess(plan, project)
    assert assessment.valid is False
    assert any(item.startswith("dependency-cycle:") for item in assessment.issues)


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
