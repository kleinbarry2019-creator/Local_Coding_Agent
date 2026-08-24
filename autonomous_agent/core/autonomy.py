"""Persistent planner, self-healing runtime, and evidence-based completion."""

from __future__ import annotations

import hashlib
import json
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


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    kind: StepKind
    tool: str
    arguments: Mapping[str, object]
    target: Path
    mutates: bool


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

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "completion": self.completion.to_dict(),
            "outputs": [dict(item) for item in self.outputs],
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
                ),
                PlanStep(
                    "step-process",
                    StepKind.TOOL,
                    "project.run-process",
                    {"argv": list(goal.argv), "cwd": str(root)},
                    root,
                    True,
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
                ),
            )
        raise ValueError("normalized goal kind is unsupported")


class FailureAnalyzer:
    def analyze(self, step: PlanStep, result: Mapping[str, object]) -> FailureAnalysis:
        category = self.classify(step, result)
        causes = {
            FailureCategory.DEPENDENCY_MISSING: "trusted capability unavailable after provisioning",
            FailureCategory.POLICY_DENIED: "policy evidence did not authorize the requested action",
            FailureCategory.TIMEOUT: "bounded tool deadline expired",
            FailureCategory.COMMAND_FAILED: "sandboxed command returned a non-zero exit status",
            FailureCategory.TOOL_FAILED: "tool handler failed at its trusted boundary",
            FailureCategory.LOOP_DETECTED: "the same failure fingerprint repeated",
        }
        return FailureAnalysis(
            category=category,
            root_cause=causes[category],
            retryable=category in {FailureCategory.TIMEOUT, FailureCategory.TOOL_FAILED},
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
        results = tuple(
            self._evaluate_criterion(item, goal, project_root, outputs, capabilities)
            for item in goal.acceptance_criteria
        )
        executed = bool(outputs) and all(item.get("success") is True for item in outputs)
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
        elif criterion.kind is CriterionKind.COMMAND_EXITED_ZERO:
            passed = any(_exit_code_zero(item) for item in outputs)
            evidence = "sandbox-exit-zero" if passed else "sandbox-command-failed"
        elif criterion.kind is CriterionKind.TOOL_AVAILABLE:
            capability = capabilities.discover(_required(criterion.target))
            passed = capability.available and capability.version is not None
            evidence = "version-probe-passed" if passed else "tool-unavailable"
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
        for index in range(start_step, len(plan)):
            step = plan[index]
            checkpoint = (
                self.checkpoints.create(session_id, step.step_id, step.target)
                if step.mutates
                and step.tool in {"project.write-file", "project.run-process"}
                else None
            )
            succeeded = False
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
                if output.get("success") is True:
                    succeeded = True
                    break
                analysis = self.failures.analyze(step, output)
                fingerprint = loops.observe(step, analysis.category, output)
                repair = self.replanner.decide(
                    analysis,
                    repeated=loops.repeated(fingerprint),
                    mutation_started=checkpoint is not None,
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
                return RuntimeResult(session_id, "failed", report, tuple(outputs))
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
        return RuntimeResult(session_id, status, report, tuple(outputs))

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
        steps.append(
            PlanStep(
                step_id=str(document["step_id"]),
                kind=StepKind(str(document["kind"])),
                tool=str(document["tool"]),
                arguments=dict(arguments),
                target=Path(str(document["target"])),
                mutates=bool(document["mutates"]),
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
    if goal.kind is GoalKind.RUN_COMMAND:
        return any(_exit_code_zero(item) for item in outputs)
    if goal.kind is GoalKind.INSTALL_TOOL:
        return capabilities.discover(_required(goal.target)).available
    return False


def _required(value: str | None) -> str:
    if value is None or not value:
        raise ValueError("required normalized goal value is missing")
    return value


def _exit_code_zero(output: Mapping[str, object]) -> bool:
    data = output.get("data")
    return isinstance(data, Mapping) and data.get("exit_code") == 0


__all__ = [
    "AutonomyRuntime",
    "CompletionEvaluator",
    "CompletionReport",
    "FailureAnalysis",
    "FailureAnalyzer",
    "FailureCategory",
    "LoopDetector",
    "PlanStep",
    "Planner",
    "RepairAction",
    "Replanner",
    "RuntimeResult",
    "StepKind",
]
