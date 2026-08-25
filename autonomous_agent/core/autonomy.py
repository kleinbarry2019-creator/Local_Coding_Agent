"""Persistent planner, self-healing runtime, and evidence-based completion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from autonomous_agent.core.capabilities import CapabilityRegistry
from autonomous_agent.core.checkpoints import CheckpointManager
from autonomous_agent.core.config import (
    AgentConfig,
    ExecutionMode,
    ensure_state_root,
    validate_state_root_isolated,
)
from autonomous_agent.core.events import AuditLog
from autonomous_agent.core.goals import (
    AcceptanceCriterion,
    CriterionKind,
    GoalKind,
    GoalNormalizer,
    NormalizedGoal,
)
from autonomous_agent.core.policy import AuthorityGrant, PolicyContext, ScopeEvidence
from autonomous_agent.core.runtime_tools import ProjectToolRuntime
from autonomous_agent.core.state import CoreStateStore
from autonomous_agent.core.system_tools import register_system_tools
from autonomous_agent.core.task_state import TaskStateStore
from autonomous_agent.core.tools import (
    ExecutionContext,
    SchemaLimits,
    ToolResult,
    ToolStatus,
)


class StepKind(str, Enum):
    TOOL = "tool"
    ENSURE_CAPABILITY = "ensure-capability"


class FailureCategory(str, Enum):
    DEPENDENCY_MISSING = "dependency-missing"
    POLICY_DENIED = "policy-denied"
    TIMEOUT = "timeout"
    COMMAND_FAILED = "command-failed"
    TOOL_FAILED = "tool-failed"
    LOOP_DETECTED = "loop-detected"


class RepairAction(str, Enum):
    RETRY = "retry"
    ROLLBACK_AND_RETRY = "rollback-and-retry"
    STOP = "stop"


@dataclass(frozen=True)
class FailureAnalysis:
    category: FailureCategory
    root_cause: str
    retryable: bool
    evidence: tuple[str, ...] = ()
    missing_capability: str | None = None
    missing_dependency: str | None = None
    candidate_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    kind: StepKind
    tool: str
    arguments: Mapping[str, object]
    target: Path
    mutates: bool
    depends_on: tuple[str, ...] = ()
    purpose: str = ""


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    passed: bool
    evidence: str


@dataclass(frozen=True)
class CompletionReport:
    completed: bool
    original_goal_matched: bool
    executed: bool
    tested: bool
    e2e_verified: bool
    criteria: tuple[CriterionResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "completed": self.completed,
            "original_goal_matched": self.original_goal_matched,
            "executed": self.executed,
            "tested": self.tested,
            "e2e_verified": self.e2e_verified,
            "criteria": [asdict(item) for item in self.criteria],
        }


@dataclass(frozen=True)
class RuntimeResult:
    session_id: str
    status: str
    completion: CompletionReport
    outputs: tuple[Mapping[str, object], ...]
    problem_solving: tuple[Mapping[str, object], ...] = ()
    plan_assessment: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "completion": self.completion.to_dict(),
            "outputs": [dict(item) for item in self.outputs],
            "problem_solving": [dict(item) for item in self.problem_solving],
            "plan_assessment": (
                None if self.plan_assessment is None else dict(self.plan_assessment)
            ),
        }


@dataclass(frozen=True)
class PlanAssessment:
    """Bounded preflight result for a plan dependency graph."""

    valid: bool
    issues: tuple[str, ...]
    ordered_step_ids: tuple[str, ...]
    risk: str

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "issues": list(self.issues),
            "ordered_step_ids": list(self.ordered_step_ids),
            "risk": self.risk,
        }


class Planner:
    """Create an explicit bounded plan from a normalized user goal."""

    def create_plan(self, goal: NormalizedGoal, project_root: Path) -> tuple[PlanStep, ...]:
        root = project_root.resolve(strict=True)
        if goal.kind is GoalKind.WRITE_FILE:
            target = root / _required(goal.target)
            return (
                PlanStep(
                    "step-write",
                    StepKind.TOOL,
                    "project.write-file",
                    {"path": str(target), "content": goal.content or ""},
                    target,
                    True,
                    purpose="create-or-replace-the-requested-file",
                ),
            )
        if goal.kind is GoalKind.READ_FILE:
            target = root / _required(goal.target)
            return (
                PlanStep(
                    "step-read",
                    StepKind.TOOL,
                    "project.read-file",
                    {"path": str(target)},
                    target,
                    False,
                    purpose="read-the-requested-file",
                ),
            )
        if goal.kind is GoalKind.LIST_FILES:
            target = root / _required(goal.target)
            return (
                PlanStep(
                    "step-list",
                    StepKind.TOOL,
                    "project.list-files",
                    {"path": str(target)},
                    target,
                    False,
                    purpose="enumerate-the-requested-directory",
                ),
            )
        if goal.kind is GoalKind.ANALYZE_PROJECT:
            return (
                PlanStep(
                    "step-analyze",
                    StepKind.TOOL,
                    "project.analyze",
                    {"path": str(root)},
                    root,
                    False,
                    purpose="map-project-languages-manifests-and-test-hints",
                ),
            )
        if goal.kind is GoalKind.RUN_COMMAND:
            executable = goal.argv[0]
            return (
                PlanStep(
                    "step-capability",
                    StepKind.ENSURE_CAPABILITY,
                    executable,
                    {"name": executable},
                    root,
                    False,
                    purpose="verify-the-command-capability",
                ),
                PlanStep(
                    "step-process",
                    StepKind.TOOL,
                    "project.run-process",
                    {"argv": list(goal.argv), "cwd": str(root)},
                    root,
                    True,
                    depends_on=("step-capability",),
                    purpose="execute-the-requested-command",
                ),
            )
        if goal.kind is GoalKind.INSTALL_TOOL:
            tool = _required(goal.target)
            return (
                PlanStep(
                    "step-install",
                    StepKind.ENSURE_CAPABILITY,
                    tool,
                    {"name": tool},
                    root,
                    True,
                    purpose="provision-and-verify-the-requested-tool",
                ),
            )
        if goal.kind is GoalKind.VM_BUILD:
            return (
                PlanStep(
                    "step-vm-hypervisor",
                    StepKind.ENSURE_CAPABILITY,
                    "qemu-system-x86_64",
                    {"name": "qemu-system-x86_64"},
                    root,
                    True,
                    purpose="research-provision-and-version-verify-the-trusted-hypervisor",
                ),
                PlanStep(
                    "step-vm-preflight",
                    StepKind.TOOL,
                    "system.vm-preflight",
                    {"project_root": str(root)},
                    root,
                    False,
                    depends_on=("step-vm-hypervisor",),
                    purpose="verify-hypervisor-kvm-iso-and-gpu-passthrough-prerequisites",
                ),
            )
        if goal.kind is GoalKind.RESEARCH_TASK:
            return (
                PlanStep(
                    "step-research-goal",
                    StepKind.TOOL,
                    "system.research-goal",
                    {"goal": goal.original, "project_root": str(root)},
                    root,
                    False,
                    purpose="research-an-unfamiliar-complex-goal-without-opening-a-browser",
                ),
            )
        raise ValueError("normalized goal kind is unsupported")

    def assess(
        self, plan: tuple[PlanStep, ...], project_root: Path
    ) -> PlanAssessment:
        """Validate dependencies, targets, and ordering before execution."""
        root = project_root.resolve(strict=True)
        issues: list[str] = []
        by_id: dict[str, PlanStep] = {}
        for step in plan:
            if not step.step_id or step.step_id in by_id:
                issues.append(f"duplicate-step-id:{step.step_id[:80]}")
                continue
            by_id[step.step_id] = step
            target = step.target.resolve(strict=False)
            if not target.is_relative_to(root):
                issues.append(f"target-outside-project:{step.step_id[:80]}")
            if step.target.exists() and step.target.is_symlink():
                issues.append(f"symlink-target:{step.step_id[:80]}")
            if step.kind is StepKind.ENSURE_CAPABILITY and not step.tool:
                issues.append(f"missing-capability-name:{step.step_id[:80]}")
            for dependency in step.depends_on:
                if dependency not in by_id and dependency not in {
                    prior.step_id for prior in plan
                }:
                    issues.append(
                        f"unknown-dependency:{step.step_id[:48]}->{dependency[:48]}"
                    )

        order: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                issues.append(f"dependency-cycle:{step_id[:80]}")
                return
            visiting.add(step_id)
            step = by_id.get(step_id)
            if step is not None:
                for dependency in step.depends_on:
                    if dependency in by_id:
                        visit(dependency)
                order.append(step_id)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in plan:
            visit(step.step_id)
        risk = "high" if any(step.mutates for step in plan) else "low"
        if any(step.kind is StepKind.ENSURE_CAPABILITY for step in plan):
            risk = "high" if risk == "high" else "medium"
        return PlanAssessment(
            valid=not issues,
            issues=tuple(dict.fromkeys(issues))[:20],
            ordered_step_ids=tuple(order),
            risk=risk,
        )


class FailureAnalyzer:
    def analyze(self, step: PlanStep, result: Mapping[str, object]) -> FailureAnalysis:
        category = self.classify(step, result)
        evidence = _failure_evidence(result)
        missing_capability = _missing_capability(step, result)
        missing_dependency = _missing_python_module(result)
        capability_blocked = _capability_install_blocked(result)
        causes = {
            FailureCategory.DEPENDENCY_MISSING: "trusted capability unavailable after provisioning",
            FailureCategory.POLICY_DENIED: "policy evidence did not authorize the requested action",
            FailureCategory.TIMEOUT: "bounded tool deadline expired",
            FailureCategory.COMMAND_FAILED: "sandboxed command returned a non-zero exit status",
            FailureCategory.TOOL_FAILED: "tool handler failed at its trusted boundary",
            FailureCategory.LOOP_DETECTED: "the same failure fingerprint repeated",
        }
        if missing_capability is not None:
            causes[FailureCategory.DEPENDENCY_MISSING] = (
                f"required executable is unavailable: {missing_capability}"
            )
        elif missing_dependency is not None:
            causes[FailureCategory.DEPENDENCY_MISSING] = (
                f"required Python module is unavailable: {missing_dependency}"
            )
        actions = {
            FailureCategory.DEPENDENCY_MISSING: ("verify-capability", "retry-step"),
            FailureCategory.POLICY_DENIED: ("inspect-policy-scope", "stop-safely"),
            FailureCategory.TIMEOUT: ("retry-with-bounded-deadline", "rollback-if-mutating"),
            FailureCategory.COMMAND_FAILED: ("inspect-command-evidence", "stop-safely"),
            FailureCategory.TOOL_FAILED: ("retry-once", "rollback-if-mutating"),
            FailureCategory.LOOP_DETECTED: ("stop-repeated-failure", "preserve-evidence"),
        }
        candidate_actions = (
            ("preserve-evidence", "stop-safely")
            if capability_blocked
            else actions[category]
        )
        return FailureAnalysis(
            category=category,
            root_cause=causes[category],
            retryable=category
            in {
                FailureCategory.DEPENDENCY_MISSING,
                FailureCategory.TIMEOUT,
                FailureCategory.TOOL_FAILED,
            }
            and not capability_blocked,
            evidence=evidence,
            missing_capability=missing_capability,
            missing_dependency=missing_dependency,
            candidate_actions=candidate_actions,
        )

    def classify(self, step: PlanStep, result: Mapping[str, object]) -> FailureCategory:
        status = result.get("status")
        diagnostic = result.get("diagnostic_code")
        if status == ToolStatus.DENIED.value:
            return FailureCategory.POLICY_DENIED
        if status == ToolStatus.TIMED_OUT.value:
            return FailureCategory.TIMEOUT
        if step.kind is StepKind.ENSURE_CAPABILITY:
            return FailureCategory.DEPENDENCY_MISSING
        data = result.get("data")
        if (
            isinstance(data, Mapping)
            and (
                _missing_capability(step, result) is not None
                or _missing_python_module(result) is not None
            )
        ):
            return FailureCategory.DEPENDENCY_MISSING
        if isinstance(data, Mapping) and data.get("exit_code") not in {None, 0}:
            return FailureCategory.COMMAND_FAILED
        if diagnostic == "internal_error":
            return FailureCategory.TOOL_FAILED
        return FailureCategory.TOOL_FAILED


class Replanner:
    """Choose a bounded recovery branch from classified root-cause evidence."""

    def decide(
        self, analysis: FailureAnalysis, *, repeated: bool, mutation_started: bool
    ) -> RepairAction:
        if repeated or not analysis.retryable:
            return RepairAction.STOP
        return (
            RepairAction.ROLLBACK_AND_RETRY
            if mutation_started
            else RepairAction.RETRY
        )


class LoopDetector:
    def __init__(self, repeat_limit: int = 2) -> None:
        self.repeat_limit = repeat_limit
        self._counts: dict[str, int] = {}

    def observe(self, step: PlanStep, category: FailureCategory, result: Mapping[str, object]) -> str:
        payload = json.dumps(
            {"step": step.step_id, "category": category.value, "result": result},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(payload).hexdigest()
        self._counts[fingerprint] = self._counts.get(fingerprint, 0) + 1
        return fingerprint

    def repeated(self, fingerprint: str) -> bool:
        return self._counts.get(fingerprint, 0) >= self.repeat_limit


class CompletionEvaluator:
    """Require independent observations for every derived criterion."""

    def evaluate(
        self,
        goal: NormalizedGoal,
        project_root: Path,
        outputs: tuple[Mapping[str, object], ...],
        capabilities: CapabilityRegistry,
    ) -> CompletionReport:
        effective_outputs = _effective_outputs(outputs)
        results = tuple(
            self._evaluate_criterion(
                item, goal, project_root, effective_outputs, capabilities
            )
            for item in goal.acceptance_criteria
        )
        executed = bool(effective_outputs) and all(
            item.get("success") is True for item in effective_outputs
        )
        e2e = any(
            item.criterion_id == "e2e" and item.passed for item in results
        )
        matched = all(item.passed for item in results)
        tested = e2e and matched
        return CompletionReport(
            completed=executed and tested and matched,
            original_goal_matched=matched,
            executed=executed,
            tested=tested,
            e2e_verified=e2e,
            criteria=results,
        )

    def _evaluate_criterion(
        self,
        criterion: AcceptanceCriterion,
        goal: NormalizedGoal,
        project_root: Path,
        outputs: tuple[Mapping[str, object], ...],
        capabilities: CapabilityRegistry,
    ) -> CriterionResult:
        passed = False
        evidence = "not-observed"
        if criterion.kind is CriterionKind.ACTION_SUCCEEDED:
            passed = bool(outputs) and all(item.get("success") is True for item in outputs)
            evidence = "all-plan-steps-succeeded" if passed else "plan-step-failed"
        elif criterion.kind is CriterionKind.FILE_EXISTS:
            path = project_root / _required(criterion.target)
            passed = path.is_file() and not path.is_symlink()
            evidence = "regular-file-observed" if passed else "file-missing"
        elif criterion.kind is CriterionKind.FILE_CONTENT_EQUALS:
            path = project_root / _required(criterion.target)
            try:
                passed = path.read_text(encoding="utf-8") == (criterion.expected or "")
            except (OSError, UnicodeError):
                passed = False
            evidence = "independent-readback-matched" if passed else "readback-mismatch"
        elif criterion.kind is CriterionKind.OUTPUT_PRODUCED:
            passed = any(item.get("data") is not None for item in outputs)
            evidence = "typed-output-observed" if passed else "output-missing"
        elif criterion.kind is CriterionKind.PROJECT_ANALYZED:
            analysis: Mapping[str, object] | None = None
            for item in outputs:
                data = item.get("data")
                if isinstance(data, Mapping) and isinstance(
                    data.get("languages"), Mapping
                ):
                    analysis = data
                    break
            passed = isinstance(analysis, Mapping) and isinstance(
                analysis.get("files"), int
            )
            evidence = (
                "project-language-map-and-structure-observed"
                if passed
                else "project-analysis-missing"
            )
        elif criterion.kind is CriterionKind.COMMAND_EXITED_ZERO:
            passed = any(_exit_code_zero(item) for item in outputs)
            evidence = "sandbox-exit-zero" if passed else "sandbox-command-failed"
        elif criterion.kind is CriterionKind.TOOL_AVAILABLE:
            capability = capabilities.discover(_required(criterion.target))
            passed = capability.available and capability.version is not None
            evidence = "version-probe-passed" if passed else "tool-unavailable"
        elif criterion.kind is CriterionKind.VM_READY:
            preflight = next(
                (
                    item.get("data")
                    for item in outputs
                    if isinstance(item.get("data"), Mapping)
                    and item.get("step_id") == "step-vm-preflight"
                ),
                None,
            )
            passed = isinstance(preflight, Mapping) and preflight.get("ready") is True
            evidence = "vm-prerequisites-verified" if passed else "vm-prerequisites-missing"
        elif criterion.kind is CriterionKind.VM_CREATED:
            passed = any(_vm_created(item) for item in outputs)
            evidence = (
                "vm-artifact-and-guest-checks-observed"
                if passed
                else "vm-creation-not-performed"
            )
        elif criterion.kind is CriterionKind.RESEARCHED:
            passed = any(_research_completed(item) for item in outputs)
            evidence = "bounded-research-plan-observed" if passed else "research-plan-missing"
        elif criterion.kind is CriterionKind.E2E_VERIFIED:
            passed = _direct_e2e(goal, project_root, outputs, capabilities)
            evidence = "public-boundary-reverified" if passed else "e2e-recheck-failed"
        return CriterionResult(criterion.criterion_id, passed, evidence)


class AutonomyRuntime:
    """Execute, persist, heal, recover, and evaluate one user task."""

    def __init__(self, config: AgentConfig) -> None:
        if config.mode is not ExecutionMode.AUTONOMOUS:
            raise ValueError("task execution requires autonomous mode")
        self.config = config
        validate_state_root_isolated(
            config.paths.project_root,
            config.paths.state_root,
        )
        state_root = ensure_state_root(config)
        self.store = CoreStateStore(
            state_root / "agent_core.sqlite3",
            state_root / "audit_anchor.json",
            state_root / "audit.lock",
        )
        self.store.initialize()
        self.audit = AuditLog(self.store)
        self.tasks = TaskStateStore(self.store, self.audit)
        self.capabilities = CapabilityRegistry(config.paths.project_root)
        self.tools = ProjectToolRuntime(config.paths.project_root).registry()
        register_system_tools(
            self.tools, self.capabilities, config.paths.project_root
        )
        self.checkpoints = CheckpointManager(config.paths.project_root, self.tasks)
        self.planner = Planner()
        self.failures = FailureAnalyzer()
        self.replanner = Replanner()
        self.completion = CompletionEvaluator()

    def run(self, raw_goal: str) -> RuntimeResult:
        goal = GoalNormalizer().normalize(raw_goal)
        plan = self.planner.create_plan(goal, self.config.paths.project_root)
        session_id = f"session-{uuid.uuid4().hex}"
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        self.audit.start_session(
            session_id, self.config.mode, self.config, now
        )
        self.tasks.create_task(
            session_id,
            raw_goal,
            _goal_document(goal),
            tuple(_step_document(step) for step in plan),
            created_at=now,
        )
        return self._execute(session_id, goal, plan, start_step=0)

    def resume(self, session_id: str) -> RuntimeResult:
        """Resume an audited incomplete task after a process crash or restart."""
        record = self.tasks.load_task(session_id)
        if record is None:
            raise ValueError("task session does not exist")
        goal = _goal_from_document(record.normalized_goal)
        plan = _plan_from_documents(record.plan)
        if record.status == "completed" and record.completion is not None:
            report = _completion_from_document(record.completion)
            return RuntimeResult(session_id, "completed", report, ())
        if record.status not in {"pending", "running", "recovering", "failed"}:
            raise ValueError("task session cannot be resumed")
        prior = tuple(
            {
                "step_id": step.step_id,
                "success": True,
                "status": "persisted-success",
                "diagnostic_code": None,
                "data": {"persisted_step": True},
            }
            for step in plan[: record.current_step]
        )
        return self._execute(
            session_id,
            goal,
            plan,
            start_step=record.current_step,
            initial_outputs=prior,
            initial_attempts=record.attempts,
        )

    def undo(self, session_id: str, step: int = 1) -> dict[str, object]:
        """Restore one of the last five durable mutation checkpoints."""
        if type(step) is not int or not 1 <= step <= 5:
            raise ValueError("undo step must be between 1 and 5")
        checkpoints = self.tasks.list_checkpoints(
            session_id, statuses=frozenset({"discarded"}), limit=5
        )
        if len(checkpoints) < step:
            raise ValueError("requested undo step is not available")
        checkpoint = checkpoints[step - 1]
        result = self.checkpoints.restore(checkpoint)
        verification = self.audit.verify()
        return {
            "session_id": session_id,
            "step": step,
            "checkpoint_id": result.checkpoint_id,
            "restored": result.restored,
            "diagnostic": result.diagnostic,
            "audit_ok": verification.ok,
            "remaining_undo": max(0, len(checkpoints) - step),
        }

    def audit_status(self) -> dict[str, object]:
        verification = self.audit.verify()
        return {
            "ok": verification.ok,
            "code": verification.code,
            "sequence": verification.sequence,
            "head_hash": verification.head_hash,
        }

    def audit_events(self, limit: int = 100) -> tuple[dict[str, object], ...]:
        """Expose bounded sanitized events for the local process viewer."""
        return self.audit.recent_events(limit=limit)

    def export_audit_log(self, session_id: str | None = None) -> dict[str, object]:
        """Persist a redacted audit snapshot beneath the project Protokolle folder."""
        if session_id is not None and (
            type(session_id) is not str or not session_id.startswith("session-")
        ):
            raise ValueError("session id is invalid")
        events = self.audit.recent_events(limit=200)
        if session_id is not None:
            events = tuple(
                event for event in events if event["session_id"] == session_id
            )
        directory = self.config.paths.project_root / "Protokolle"
        if directory.exists() and directory.is_symlink():
            raise ValueError("audit directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        filename = f"audit-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
        destination = directory / filename
        descriptor, temporary_name = tempfile.mkstemp(prefix=".audit-", dir=directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            payload = json.dumps(
                {"session_id": session_id, "events": list(events)},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return {
            "path": destination.relative_to(self.config.paths.project_root).as_posix(),
            "event_count": len(events),
        }

    def _execute(
        self,
        session_id: str,
        goal: NormalizedGoal,
        plan: tuple[PlanStep, ...],
        *,
        start_step: int,
        initial_outputs: tuple[Mapping[str, object], ...] = (),
        initial_attempts: int = 0,
    ) -> RuntimeResult:
        outputs: list[Mapping[str, object]] = list(initial_outputs)
        loops = LoopDetector()
        attempts = initial_attempts
        assessment = self.planner.assess(plan, self.config.paths.project_root)
        problem_solving: list[Mapping[str, object]] = [
            {
                "phase": "planning",
                "strategy": "preflight-dependency-graph",
                "valid": assessment.valid,
                "risk": assessment.risk,
                "ordered_step_ids": list(assessment.ordered_step_ids),
                "issues": list(assessment.issues),
            }
        ]
        if not assessment.valid:
            report = self.completion.evaluate(
                goal,
                self.config.paths.project_root,
                tuple(outputs),
                self.capabilities,
            )
            self.tasks.transition(
                session_id,
                status="failed",
                current_step=start_step,
                attempts=attempts,
                completion=report.to_dict(),
                outcome="plan-preflight-failed",
            )
            return RuntimeResult(
                session_id,
                "failed",
                report,
                tuple(outputs),
                tuple(problem_solving),
                assessment.to_dict(),
            )
        for index in range(start_step, len(plan)):
            step = plan[index]
            checkpoint = (
                self.checkpoints.create(session_id, step.step_id, step.target)
                if step.mutates
                and step.tool in {"project.write-file", "project.run-process"}
                else None
            )
            succeeded = False
            recovery_attempted = False
            for _attempt in range(3):
                attempts += 1
                self.tasks.transition(
                    session_id,
                    status="running",
                    current_step=index,
                    attempts=attempts,
                    outcome="step-running",
                )
                output = self._execute_step(session_id, step)
                outputs.append(output)
                if step.kind is StepKind.ENSURE_CAPABILITY:
                    research = output.get("data")
                    if isinstance(research, Mapping) and isinstance(
                        research.get("research_source"), str
                    ):
                        problem_solving.append(
                            {
                                "phase": "research",
                                "step_id": step.step_id,
                                "capability": step.tool,
                                "source": research["research_source"],
                                "package_manager": research.get("package_manager"),
                                "package": research.get("package"),
                                "reboot_required": research.get("reboot_required"),
                                "outcome": (
                                    "available"
                                    if output.get("success") is True
                                    else "blocked-or-unavailable"
                                ),
                            }
                        )
                if step.tool == "system.research-goal":
                    research = output.get("data")
                    if isinstance(research, Mapping) and research.get(
                        "research_completed"
                    ) is True:
                        problem_solving.append(
                            {
                                "phase": "research",
                                "step_id": step.step_id,
                                "goal_class": research.get("goal_class"),
                                "browser_opened": research.get("browser_opened"),
                                "network_used": research.get("network_used"),
                                "outcome": "bounded-plan-created",
                            }
                        )
                if output.get("success") is True:
                    succeeded = True
                    break
                analysis = self.failures.analyze(step, output)
                fingerprint = loops.observe(step, analysis.category, output)
                problem_solving.append(
                    {
                        "phase": "diagnosis",
                        "step_id": step.step_id,
                        "category": analysis.category.value,
                        "root_cause": analysis.root_cause,
                        "evidence": list(analysis.evidence),
                        "missing_dependency": analysis.missing_dependency,
                        "candidate_actions": list(analysis.candidate_actions),
                    }
                )
                if analysis.missing_dependency is not None:
                    problem_solving.append(
                        {
                            "phase": "research",
                            "step_id": step.step_id,
                            "dependency": analysis.missing_dependency,
                            "source": "local-runtime-diagnostics",
                            "browser_opened": False,
                            "outcome": "package-policy-required-before-install",
                        }
                    )
                if (
                    step.kind is StepKind.TOOL
                    and analysis.missing_capability is not None
                    and not recovery_attempted
                ):
                    recovery_attempted = True
                    recovery_step = PlanStep(
                        step_id=f"recover-capability-{analysis.missing_capability}",
                        kind=StepKind.ENSURE_CAPABILITY,
                        tool=analysis.missing_capability,
                        arguments={"name": analysis.missing_capability},
                        target=self.config.paths.project_root,
                        mutates=False,
                    )
                    recovery_output = self._execute_step(session_id, recovery_step)
                    outputs.append(recovery_output)
                    recovery_ok = recovery_output.get("success") is True
                    problem_solving.append(
                        {
                            "phase": "replan",
                            "step_id": step.step_id,
                            "strategy": "provision-missing-capability",
                            "capability": analysis.missing_capability,
                            "outcome": "verified" if recovery_ok else "unavailable",
                        }
                    )
                    if recovery_ok:
                        self.tasks.transition(
                            session_id,
                            status="recovering",
                            current_step=index,
                            attempts=attempts,
                            failure_fingerprint=fingerprint,
                            outcome="capability-provisioned-retry",
                        )
                        continue
                repair = (
                    RepairAction.STOP
                    if (
                        analysis.missing_dependency is not None
                        or _capability_install_blocked(output)
                    )
                    else self.replanner.decide(
                        analysis,
                        repeated=loops.repeated(fingerprint),
                        mutation_started=checkpoint is not None,
                    )
                )
                problem_solving.append(
                    {
                        "phase": "replan",
                        "step_id": step.step_id,
                        "strategy": repair.value,
                        "outcome": "continue" if repair is not RepairAction.STOP else "stop",
                        "reason": analysis.root_cause,
                    }
                )
                self.tasks.transition(
                    session_id,
                    status="recovering",
                    current_step=index,
                    attempts=attempts,
                    failure_fingerprint=fingerprint,
                    outcome=analysis.category.value,
                )
                if checkpoint is not None and repair is RepairAction.ROLLBACK_AND_RETRY:
                    self.checkpoints.restore(checkpoint)
                    checkpoint = self.checkpoints.create(
                        session_id, step.step_id, step.target
                    )
                elif checkpoint is not None and repair is RepairAction.STOP:
                    self.checkpoints.restore(checkpoint)
                    checkpoint = None
                if repair is RepairAction.STOP:
                    break
            if checkpoint is not None:
                self.checkpoints.discard(checkpoint)
            if not succeeded:
                report = self.completion.evaluate(
                    goal,
                    self.config.paths.project_root,
                    tuple(outputs),
                    self.capabilities,
                )
                self.tasks.transition(
                    session_id,
                    status="failed",
                    current_step=index,
                    attempts=attempts,
                    completion=report.to_dict(),
                    outcome="verified-failure",
                )
                return RuntimeResult(
                    session_id,
                    "failed",
                    report,
                    tuple(outputs),
                    tuple(problem_solving),
                    assessment.to_dict(),
                )
            self.tasks.transition(
                session_id,
                status="running",
                current_step=index + 1,
                attempts=attempts,
                outcome="step-succeeded",
            )
        report = self.completion.evaluate(
            goal,
            self.config.paths.project_root,
            tuple(outputs),
            self.capabilities,
        )
        status = "completed" if report.completed else "failed"
        self.tasks.transition(
            session_id,
            status=status,
            current_step=len(plan),
            attempts=attempts,
            completion=report.to_dict(),
            outcome="verified-complete" if report.completed else "completion-rejected",
        )
        return RuntimeResult(
            session_id,
            status,
            report,
            tuple(outputs),
            tuple(problem_solving),
            assessment.to_dict(),
        )

    def _execute_step(self, session_id: str, step: PlanStep) -> Mapping[str, object]:
        if step.kind is StepKind.ENSURE_CAPABILITY:
            current = self.capabilities.discover(step.tool)
            if current.available:
                return {
                    "step_id": step.step_id,
                    "success": True,
                    "status": "ok",
                    "diagnostic_code": "already-available",
                    "data": {
                        "name": current.name,
                        "installed": True,
                        "executable": (
                            None if current.executable is None else str(current.executable)
                        ),
                        "version": current.version,
                    },
                }
            arguments: Mapping[str, object] = {
                "name": step.tool,
                "project_root": str(self.config.paths.project_root),
            }
            context = self._execution_context(session_id, step.target)
            request = self.tools.policy_request(
                "system.ensure-tool", arguments, context
            )
            now = datetime.now(UTC)
            grant = AuthorityGrant(
                grant_id=f"grant-{uuid.uuid4().hex}",
                capabilities=request.capabilities,
                action_digest=request.request_id,
                issued_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(minutes=2),
            )
            authorized = replace(
                context, policy=replace(context.policy, authority=grant)
            )
            return _tool_output(
                step,
                self.tools.execute("system.ensure-tool", arguments, authorized),
            )
        context = self._execution_context(session_id, step.target)
        tool_result = self.tools.execute(step.tool, step.arguments, context)
        return _tool_output(step, tool_result)

    def _execution_context(
        self, session_id: str, target: Path
    ) -> ExecutionContext:
        return ExecutionContext(
            policy=PolicyContext(
                mode=self.config.mode,
                canonical_project_root=self.config.paths.project_root,
                scope=ScopeEvidence(
                    project_root=self.config.paths.project_root,
                    resolved_targets=(target,),
                    resolver_id="trusted-path-resolver-v1",
                    valid=True,
                ),
                hard_limits=self.config.limits,
                authority=None,
                recovery=None,
                doctor_capabilities=frozenset(
                    {"doctor.read", "doctor.ollama.loopback"}
                ),
            ),
            deadline_monotonic=time.monotonic()
            + self.config.limits.hard_command_timeout_s,
            schema_limits=SchemaLimits(
                max_output_bytes=self.config.limits.hard_max_output_bytes
            ),
            session_id=session_id,
        )


def _tool_output(step: PlanStep, result: ToolResult) -> Mapping[str, object]:
    success = result.status is ToolStatus.OK
    if (
        success
        and isinstance(result.data, Mapping)
        and "exit_code" in result.data
        and result.data["exit_code"] != 0
    ):
        success = False
    if (
        success
        and step.kind is StepKind.ENSURE_CAPABILITY
        and isinstance(result.data, Mapping)
        and result.data.get("installed") is not True
    ):
        success = False
    return {
        "step_id": step.step_id,
        "success": success,
        "status": result.status.value,
        "diagnostic_code": result.diagnostic_code,
        "diagnostic": result.diagnostic,
        "duration_ms": result.duration_ms,
        "data": None if result.data is None else dict(result.data),
    }


def _failure_evidence(result: Mapping[str, object]) -> tuple[str, ...]:
    """Extract bounded, non-secret diagnostics for explainable recovery."""
    evidence: list[str] = []
    diagnostic_code = result.get("diagnostic_code")
    if isinstance(diagnostic_code, str) and diagnostic_code:
        evidence.append(f"diagnostic:{diagnostic_code[:80]}")
    data = result.get("data")
    if isinstance(data, Mapping):
        exit_code = data.get("exit_code")
        if type(exit_code) is int:
            evidence.append(f"exit-code:{exit_code}")
        for name in ("stderr", "stdout"):
            value = data.get(name)
            if isinstance(value, str) and value.strip():
                compact = " ".join(value.split())[:240]
                evidence.append(f"{name}:{compact}")
    return tuple(evidence[:6])


def _effective_outputs(
    outputs: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    """Keep the last observation for each step after bounded retries/replans."""
    latest: dict[str, tuple[int, Mapping[str, object]]] = {}
    unkeyed: list[tuple[int, Mapping[str, object]]] = []
    for index, output in enumerate(outputs):
        step_id = output.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            unkeyed.append((index, output))
            continue
        latest[step_id] = (index, output)
    selected = [*unkeyed, *latest.values()]
    selected.sort(key=lambda item: item[0])
    return tuple(item[1] for item in selected)


def _missing_capability(
    step: PlanStep, result: Mapping[str, object]
) -> str | None:
    """Identify a safe executable candidate from a command-not-found result."""
    if step.kind is StepKind.ENSURE_CAPABILITY:
        return step.tool
    data = result.get("data")
    parts: list[str] = []
    if isinstance(data, Mapping):
        for name in ("stderr", "stdout"):
            value = data.get(name)
            if isinstance(value, str):
                parts.append(value)
        exit_code = data.get("exit_code")
        if exit_code == 127:
            parts.append("exit-code-127")
    diagnostic = result.get("diagnostic")
    if isinstance(diagnostic, str):
        parts.append(diagnostic)
    text = "\n".join(parts)
    match = re.search(
        r"(?:^|[:\s])([A-Za-z0-9][A-Za-z0-9._+-]{0,63}):\s*(?:command not found|not found)"
        r"|(?:command not found[: ]+|not found[: ]+|No such file or directory[: ]*)"
        r"([A-Za-z0-9][A-Za-z0-9._+-]{0,63})",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    candidate = match.group(1) or match.group(2)
    if candidate is None:
        return None
    if candidate in {"command", "file", "directory"}:
        return None
    return candidate


def _missing_python_module(result: Mapping[str, object]) -> str | None:
    data = result.get("data")
    if not isinstance(data, Mapping):
        return None
    text = "\n".join(
        value for name in ("stderr", "stdout")
        if isinstance(value := data.get(name), str)
    )
    match = re.search(
        r"No module named ['\"]?([A-Za-z0-9_][A-Za-z0-9_.-]{0,63})",
        text,
        flags=re.IGNORECASE,
    )
    return None if match is None else match.group(1)


def _capability_install_blocked(result: Mapping[str, object]) -> bool:
    """Return true when trusted research explicitly forbids unattended install."""
    data = result.get("data")
    if not isinstance(data, Mapping):
        return False
    return data.get("diagnostic") in {
        "untrusted-or-unsupported-tool",
        "installed-reboot-required",
    } or (
        data.get("research_source") == "host-profile"
        and data.get("package_manager") == "rpm-ostree"
    )


def _goal_document(goal: NormalizedGoal) -> dict[str, object]:
    return {
        "original": goal.original,
        "kind": goal.kind.value,
        "summary": goal.summary,
        "target": goal.target,
        "content": goal.content,
        "argv": list(goal.argv),
        "implicit_requirements": list(goal.implicit_requirements),
        "acceptance_criteria": [
            {
                "criterion_id": item.criterion_id,
                "kind": item.kind.value,
                "target": item.target,
                "expected": item.expected,
            }
            for item in goal.acceptance_criteria
        ],
    }


def _step_document(step: PlanStep) -> dict[str, object]:
    return {
        "step_id": step.step_id,
        "kind": step.kind.value,
        "tool": step.tool,
        "arguments": dict(step.arguments),
        "target": str(step.target),
        "mutates": step.mutates,
        "depends_on": list(step.depends_on),
        "purpose": step.purpose,
    }


def _goal_from_document(document: Mapping[str, object]) -> NormalizedGoal:
    raw_criteria = document.get("acceptance_criteria")
    raw_argv = document.get("argv")
    raw_requirements = document.get("implicit_requirements")
    if (
        not isinstance(raw_criteria, list)
        or not isinstance(raw_argv, list)
        or not isinstance(raw_requirements, list)
    ):
        raise TypeError("persisted normalized goal is invalid")
    criteria = tuple(
        AcceptanceCriterion(
            criterion_id=str(item["criterion_id"]),
            kind=CriterionKind(str(item["kind"])),
            target=None if item.get("target") is None else str(item["target"]),
            expected=None if item.get("expected") is None else str(item["expected"]),
        )
        for item in raw_criteria
        if isinstance(item, dict)
    )
    return NormalizedGoal(
        original=str(document["original"]),
        kind=GoalKind(str(document["kind"])),
        summary=str(document["summary"]),
        target=None if document.get("target") is None else str(document["target"]),
        content=None if document.get("content") is None else str(document["content"]),
        argv=tuple(str(item) for item in raw_argv),
        implicit_requirements=tuple(str(item) for item in raw_requirements),
        acceptance_criteria=criteria,
    )


def _plan_from_documents(
    documents: tuple[Mapping[str, object], ...],
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    for document in documents:
        arguments = document.get("arguments")
        if not isinstance(arguments, Mapping):
            raise TypeError("persisted plan arguments are invalid")
        raw_dependencies = document.get("depends_on", [])
        if not isinstance(raw_dependencies, list):
            raise TypeError("persisted plan dependencies are invalid")
        steps.append(
            PlanStep(
                step_id=str(document["step_id"]),
                kind=StepKind(str(document["kind"])),
                tool=str(document["tool"]),
                arguments=dict(arguments),
                target=Path(str(document["target"])),
                mutates=bool(document["mutates"]),
                depends_on=tuple(str(item) for item in raw_dependencies),
                purpose=str(document.get("purpose", "")),
            )
        )
    return tuple(steps)


def _completion_from_document(document: Mapping[str, object]) -> CompletionReport:
    raw_criteria = document.get("criteria")
    if not isinstance(raw_criteria, list):
        raise TypeError("persisted completion is invalid")
    criteria = tuple(
        CriterionResult(
            criterion_id=str(item["criterion_id"]),
            passed=bool(item["passed"]),
            evidence=str(item["evidence"]),
        )
        for item in raw_criteria
        if isinstance(item, dict)
    )
    return CompletionReport(
        completed=bool(document.get("completed")),
        original_goal_matched=bool(document.get("original_goal_matched")),
        executed=bool(document.get("executed")),
        tested=bool(document.get("tested")),
        e2e_verified=bool(document.get("e2e_verified")),
        criteria=criteria,
    )


def _direct_e2e(
    goal: NormalizedGoal,
    project_root: Path,
    outputs: tuple[Mapping[str, object], ...],
    capabilities: CapabilityRegistry,
) -> bool:
    if not outputs or not all(item.get("success") is True for item in outputs):
        return False
    if goal.kind is GoalKind.WRITE_FILE:
        path = project_root / _required(goal.target)
        try:
            return path.is_file() and path.read_text(encoding="utf-8") == (goal.content or "")
        except (OSError, UnicodeError):
            return False
    if goal.kind in {GoalKind.READ_FILE, GoalKind.LIST_FILES}:
        return any(item.get("data") is not None for item in outputs)
    if goal.kind is GoalKind.ANALYZE_PROJECT:
        for item in outputs:
            data = item.get("data")
            if isinstance(data, Mapping) and isinstance(
                data.get("languages"), Mapping
            ) and isinstance(data.get("files"), int):
                return True
        return False
    if goal.kind is GoalKind.RUN_COMMAND:
        return any(_exit_code_zero(item) for item in outputs)
    if goal.kind is GoalKind.INSTALL_TOOL:
        return capabilities.discover(_required(goal.target)).available
    if goal.kind is GoalKind.VM_BUILD:
        return any(_vm_created(item) for item in outputs)
    if goal.kind is GoalKind.RESEARCH_TASK:
        return False
    return False


def _required(value: str | None) -> str:
    if value is None or not value:
        raise ValueError("required normalized goal value is missing")
    return value


def _exit_code_zero(output: Mapping[str, object]) -> bool:
    data = output.get("data")
    return isinstance(data, Mapping) and data.get("exit_code") == 0


def _vm_created(output: Mapping[str, object]) -> bool:
    data = output.get("data")
    return (
        isinstance(data, Mapping)
        and data.get("created") is True
        and data.get("verified") is True
    )


def _research_completed(output: Mapping[str, object]) -> bool:
    data = output.get("data")
    return isinstance(data, Mapping) and data.get("research_completed") is True


__all__ = [
    "AutonomyRuntime",
    "CompletionEvaluator",
    "CompletionReport",
    "FailureAnalysis",
    "FailureAnalyzer",
    "FailureCategory",
    "LoopDetector",
    "PlanAssessment",
    "PlanStep",
    "Planner",
    "RepairAction",
    "Replanner",
    "RuntimeResult",
    "StepKind",
]
