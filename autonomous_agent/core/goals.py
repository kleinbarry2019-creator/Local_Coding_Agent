"""Deterministic normalization for simple natural-language agent goals."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_MAX_GOAL_BYTES = 32_768
_PATH_TOKEN = re.compile(r"(?:^|\s)([`\"']?)([^\s`\"']+\.[A-Za-z0-9._-]+)\1")


class GoalError(ValueError):
    """A stable, detail-bounded goal normalization error."""


class GoalKind(str, Enum):
    WRITE_FILE = "write-file"
    READ_FILE = "read-file"
    LIST_FILES = "list-files"
    ANALYZE_PROJECT = "analyze-project"
    RUN_COMMAND = "run-command"
    INSTALL_TOOL = "install-tool"
    VM_BUILD = "vm-build"
    RESEARCH_TASK = "research-task"


class CriterionKind(str, Enum):
    ACTION_SUCCEEDED = "action-succeeded"
    FILE_EXISTS = "file-exists"
    FILE_CONTENT_EQUALS = "file-content-equals"
    OUTPUT_PRODUCED = "output-produced"
    PROJECT_ANALYZED = "project-analyzed"
    COMMAND_EXITED_ZERO = "command-exited-zero"
    TOOL_AVAILABLE = "tool-available"
    VM_READY = "vm-ready"
    VM_CREATED = "vm-created"
    RESEARCHED = "researched"
    E2E_VERIFIED = "e2e-verified"


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    kind: CriterionKind
    target: str | None = None
    expected: str | None = None


@dataclass(frozen=True)
class NormalizedGoal:
    original: str
    kind: GoalKind
    summary: str
    target: str | None
    content: str | None
    argv: tuple[str, ...]
    implicit_requirements: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]


class GoalNormalizer:
    """Interpret a deliberately bounded set of simple German/English requests."""

    def normalize(self, raw_goal: str) -> NormalizedGoal:
        goal = _validated_goal(raw_goal)
        lowered = goal.casefold()
        if self._is_windows_vm_request(lowered):
            return self._vm_build(goal)
        if self._is_complex_research_request(lowered):
            return self._research_task(goal)
        if _starts_with(lowered, ("install ", "installiere ")):
            return self._install(goal)
        if _starts_with(
            lowered,
            ("run ", "execute ", "führe ", "fuehre ", "starte "),
        ):
            return self._run(goal)
        if _contains_word(lowered, ("create", "write", "erstelle", "schreibe")):
            return self._write(goal)
        if _contains_word(lowered, ("read", "show", "lies", "zeige")):
            return self._read(goal)
        if _contains_word(lowered, ("list", "liste", "auflisten")):
            return self._list(goal)
        if _contains_word(
            lowered,
            (
                "analyze",
                "analyse",
                "analysiere",
                "inspect",
                "untersuche",
                "understand",
                "verstehe",
            ),
        ) and _contains_word(lowered, ("project", "projekt", "repo", "repository", "code")):
            return self._analyze(goal)
        raise GoalError("goal is not a supported simple coding or system task")

    @staticmethod
    def _is_windows_vm_request(lowered: str) -> bool:
        return (
            _contains_word(lowered, ("windows",))
            and _contains_word(
                lowered,
                ("vm", "virtual machine", "virtuelle maschine", "virtualisierung"),
            )
            and _contains_word(
                lowered,
                ("baue", "bauen", "erstelle", "erstellen", "build", "create"),
            )
        )

    def _vm_build(self, goal: str) -> NormalizedGoal:
        return _goal(
            goal,
            GoalKind.VM_BUILD,
            "Build and verify a Windows VM with bounded hardware preflight",
            target="windows-vm",
            implicit_requirements=(
                "QEMU/KVM or an equivalent trusted hypervisor",
                "a user-provided Windows installation ISO and license",
                "IOMMU/VFIO support for dedicated GPU passthrough",
                "a bounded VM disk and memory allocation",
            ),
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("preflight", CriterionKind.VM_READY),
                AcceptanceCriterion("created", CriterionKind.VM_CREATED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    @staticmethod
    def _is_complex_research_request(lowered: str) -> bool:
        return _contains_word(
            lowered,
            (
                "rest-api",
                "rest api",
                "docker-deployment",
                "datenbank",
                "database",
                "migrationen",
                "kubernetes",
                "deployment",
                "microservice",
                "machine learning",
                "machine-learning",
                "deep learning",
                "android-app",
                "android app",
                "ios-app",
                "ios app",
                "mobile app",
                "desktop app",
                "browser extension",
                "frontend",
                "react",
                "vue",
                "terraform",
                "ansible",
                "ci/cd",
                "pipeline",
                "distributed system",
                "verteilte datenpipeline",
                "compiler",
                "bytecode",
                "lexer",
                "parser",
                "multimodal",
                "3d-anwendung",
                "3d anwendung",
                "echtzeit-rendering",
                "audioverarbeitung",
                "gpu-beschleunigung",
                "blockchain",
                "computer vision",
                "sprachassistent",
                "speech recognition",
                "security audit",
                "sicherheits-audit",
                "sicherheitsaudit",
                "sicherheitsprüfung",
                "sicherheitsanalyse",
                "web-sicherheitsanalyse",
                "web-sicherheit",
                "bedrohungsmodell",
                "penetrationstest",
                "pentest",
                "zero-trust",
                "zero trust",
                "netzwerkarchitektur",
                "betriebssystem",
                "betriebssystemkern",
                "container",
                "containerisierte",
                "infrastruktur",
                "service mesh",
                "gpu-simulation",
                "simulation",
                "wissenschaftliche simulation",
            ),
        ) and _contains_word(
            lowered,
            (
                "baue",
                "bauen",
                "erstelle",
                "erstellen",
                "implementiere",
                "implement",
                "entwickle",
                "entwickeln",
                "konzipiere",
                "konzipieren",
                "entwirf",
                "entwerfen",
                "deploy",
                "automatisiere",
                "migriere",
                "migrieren",
                "analysiere",
                "analysieren",
                "prüfe",
                "pruefe",
                "führe",
                "fuehre",
                "durch",
                "plane",
                "planen",
                "setze",
                "umsetze",
                "umsetzen",
                "build",
                "create",
            ),
        )

    def _research_task(self, goal: str) -> NormalizedGoal:
        return _goal(
            goal,
            GoalKind.RESEARCH_TASK,
            "Research and bound an unfamiliar complex task before execution",
            target="complex-task",
            implicit_requirements=(
                "Research uses the local capability matrix and trusted background sources without opening a browser.",
                "Do not claim execution until an implementation plan and independent E2E evidence exist.",
            ),
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("research", CriterionKind.RESEARCHED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _write(self, goal: str) -> NormalizedGoal:
        target = _extract_path(goal)
        content = _extract_content(goal)
        if target is None:
            raise GoalError("write goal requires an explicit relative file target")
        criteria = (
            AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
            AcceptanceCriterion("exists", CriterionKind.FILE_EXISTS, target),
            AcceptanceCriterion(
                "content",
                CriterionKind.FILE_CONTENT_EQUALS,
                target,
                content,
            ),
            AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
        )
        return _goal(
            goal,
            GoalKind.WRITE_FILE,
            f"Write {target}",
            target=target,
            content=content,
            criteria=criteria,
        )

    def _read(self, goal: str) -> NormalizedGoal:
        target = _extract_path(goal)
        if target is None:
            raise GoalError("read goal requires an explicit relative file target")
        return _goal(
            goal,
            GoalKind.READ_FILE,
            f"Read {target}",
            target=target,
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("output", CriterionKind.OUTPUT_PRODUCED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _list(self, goal: str) -> NormalizedGoal:
        target = _extract_directory(goal)
        return _goal(
            goal,
            GoalKind.LIST_FILES,
            f"List files in {target}",
            target=target,
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("output", CriterionKind.OUTPUT_PRODUCED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _analyze(self, goal: str) -> NormalizedGoal:
        return _goal(
            goal,
            GoalKind.ANALYZE_PROJECT,
            "Analyze project structure, languages, and test hints",
            target=".",
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("analysis", CriterionKind.PROJECT_ANALYZED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _run(self, goal: str) -> NormalizedGoal:
        command = _after_command_verb(goal)
        try:
            argv = tuple(shlex.split(command, posix=True))
        except ValueError as error:
            raise GoalError("command quoting is invalid") from error
        if not argv or any("\x00" in item for item in argv):
            raise GoalError("run goal requires a command and arguments")
        return _goal(
            goal,
            GoalKind.RUN_COMMAND,
            f"Run {argv[0]}",
            argv=argv,
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("exit", CriterionKind.COMMAND_EXITED_ZERO),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _install(self, goal: str) -> NormalizedGoal:
        words = goal.split(maxsplit=1)
        tool = words[1].strip() if len(words) == 2 else ""
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,63}", tool):
            raise GoalError("install goal requires one trusted tool name")
        return _goal(
            goal,
            GoalKind.INSTALL_TOOL,
            f"Install and verify {tool}",
            target=tool,
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion(
                    "available", CriterionKind.TOOL_AVAILABLE, tool
                ),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )


def _validated_goal(raw_goal: object) -> str:
    if type(raw_goal) is not str:
        raise GoalError("goal must be text")
    goal = raw_goal.strip()
    if (
        not goal
        or "\x00" in goal
        or len(goal.encode("utf-8")) > _MAX_GOAL_BYTES
    ):
        raise GoalError("goal is empty or exceeds its bounded size")
    return goal


def _starts_with(value: str, prefixes: tuple[str, ...]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def _contains_word(value: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", value) for word in words)


def _extract_path(goal: str) -> str | None:
    backtick = re.search(r"`([^`]+)`", goal)
    candidates = [backtick.group(1)] if backtick else []
    candidates.extend(match.group(2) for match in _PATH_TOKEN.finditer(goal))
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:")
        path = Path(candidate)
        if not path.is_absolute() and ".." not in path.parts:
            return str(path)
    return None


def _extract_content(goal: str) -> str:
    markers = (
        r"\bwith\s+content\s+",
        r"\bcontaining\s+",
        r"\bmit\s+dem\s+inhalt\s+",
        r"\bmit\s+inhalt\s+",
    )
    for marker in markers:
        match = re.search(marker, goal, flags=re.IGNORECASE)
        if match:
            value = goal[match.end() :].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"`":
                value = value[1:-1]
            return value
    return ""


def _extract_directory(goal: str) -> str:
    backtick = re.search(r"`([^`]+)`", goal)
    if backtick:
        candidate = Path(backtick.group(1))
        if not candidate.is_absolute() and ".." not in candidate.parts:
            return str(candidate)
    return "."


def _after_command_verb(goal: str) -> str:
    lowered = goal.casefold()
    for prefix in ("run ", "execute ", "starte "):
        if lowered.startswith(prefix):
            return goal[len(prefix) :].strip()
    for prefix in ("führe ", "fuehre "):
        if lowered.startswith(prefix):
            command = goal[len(prefix) :].strip()
            return re.sub(r"\s+aus\s*$", "", command, flags=re.IGNORECASE)
    return ""


def _goal(
    original: str,
    kind: GoalKind,
    summary: str,
    *,
    target: str | None = None,
    content: str | None = None,
    argv: tuple[str, ...] = (),
    implicit_requirements: tuple[str, ...] = (),
    criteria: tuple[AcceptanceCriterion, ...],
) -> NormalizedGoal:
    return NormalizedGoal(
        original=original,
        kind=kind,
        summary=summary,
        target=target,
        content=content,
        argv=argv,
        implicit_requirements=(
            *implicit_requirements,
            "Stay inside the canonical project scope.",
            "Preserve an auditable persistent task state.",
            "Recover or roll back safely after a failed mutation.",
            "Do not report completion until every acceptance criterion is observed.",
        ),
        acceptance_criteria=criteria,
    )


__all__ = [
    "AcceptanceCriterion",
    "CriterionKind",
    "GoalError",
    "GoalKind",
    "GoalNormalizer",
    "NormalizedGoal",
]
