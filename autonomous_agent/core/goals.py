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
    SECURITY_SCAN = "security-scan"
    HOST_SECURITY_SCAN = "host-security-scan"
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
        if self._is_security_scan_request(lowered):
            return self._security_scan(goal)
        if self._is_host_security_request(lowered):
            return self._host_security_scan(goal)
        if self._is_windows_vm_request(lowered):
            return self._vm_build(goal)
        if self._is_complex_research_request(lowered):
            return self._research_task(goal)
        if self._is_research_execution_request(lowered):
            return self._research_task(goal)
        if self._is_research_artifact_request(lowered):
            # A research request that promises a report/file is not a bounded
            # background-research task.  Keep it in the complex workflow so
            # the completion evaluator cannot claim success without the
            # requested artifact actually being produced and verified.
            return self._research_task(goal)
        if self._is_standalone_research_request(lowered):
            return self._research_task(goal, research_only=True)
        if self._is_mixed_read_write_request(lowered):
            return self._research_task(goal)
        if _starts_with(lowered, ("install ", "installiere ")):
            if self._is_install_compound_request(lowered):
                return self._research_task(goal)
            return self._install(goal)
        if _starts_with(
            lowered,
            ("run ", "execute ", "führe ", "fuehre ", "starte "),
        ):
            if self._is_ambitious_unknown_request(lowered) and not self._looks_like_explicit_command(
                goal
            ):
                return self._research_task(goal)
            return self._run(goal)
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
        ) and _contains_word(
            lowered, ("write", "create", "erstelle", "erstellen", "schreibe")
        ):
            return self._research_task(goal)
        if _contains_word(
            lowered,
            ("create", "write", "erstelle", "erstellen", "schreibe", "anlegen", "erzeuge"),
        ):
            if self._is_ambitious_unknown_request(lowered) and not _extract_content(goal):
                return self._research_task(goal)
            try:
                return self._write(goal)
            except GoalError:
                if self._is_ambitious_unknown_request(lowered):
                    return self._research_task(goal)
                raise
        if _contains_word(lowered, ("read", "show", "lies", "zeige")):
            return self._read(goal)
        if _contains_word(lowered, ("list", "liste", "auflisten")):
            return self._list(goal)
        analyze_intent = _contains_word(
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
        )
        if analyze_intent and _contains_word(
            lowered, ("write", "create", "erstelle", "erstellen", "schreibe")
        ):
            return self._research_task(goal)
        if analyze_intent and _contains_word(
            lowered, ("project", "projekt", "repo", "repository", "code")
        ):
            return self._analyze(goal)
        if self._is_natural_run_request(lowered):
            try:
                return self._run_natural(goal)
            except GoalError:
                if self._is_ambitious_unknown_request(lowered):
                    return self._research_task(goal)
                raise
        if self._is_ambitious_unknown_request(lowered):
            return self._research_task(goal)
        raise GoalError("goal is not a supported simple coding or system task")

    @staticmethod
    def _is_security_scan_request(lowered: str) -> bool:
        scan_intent = _contains_word(
            lowered,
            (
                "security scan",
                "sicherheits-scan",
                "sicherheitsscan",
                "sicherheitsprüfung",
                "security audit",
                "sicherheitsanalyse",
            ),
        )
        scope_hint = any(
            token in lowered for token in ("project", "projekt", "repo", "repository", "code")
        )
        return scan_intent and scope_hint and not _contains_word(
            lowered,
            ("fix", "behebe", "beheben", "implement", "implementiere", "apply", "anwenden"),
        )

    @staticmethod
    def _is_host_security_request(lowered: str) -> bool:
        security_intent = any(
            token in lowered
            for token in (
                "systemsicherheit",
                "system security",
                "host security",
                "netzwerksicherheit",
                "network security",
                "sicherheit des systems",
            )
        )
        inspection_intent = any(
            token in lowered
            for token in (
                "scan",
                "prüfung",
                "pruefung",
                "audit",
                "prüfe",
                "pruefe",
                "untersuche",
                "ports",
                "prozesse",
                "processes",
            )
        )
        return security_intent and inspection_intent and not _contains_word(
            lowered,
            ("fix", "behebe", "beheben", "implement", "implementiere", "apply", "anwenden"),
        )

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
        ) or (
            _contains_word(lowered, ("windows",))
            and _contains_word(
                lowered,
                ("vm", "virtual machine", "virtuelle maschine", "virtualisierung"),
            )
            and _contains_word(lowered, ("installiere", "richte", "qemu"))
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
    def _is_standalone_research_request(lowered: str) -> bool:
        if lowered in {"research", "recherche", "recherchiere"}:
            return True
        return lowered.startswith(
            (
                "research ",
                "recherche ",
                "recherchiere ",
                "recherchen ",
                "hintergrundrecherche ",
                "deep research ",
            )
        ) or _contains_word(
            lowered,
            (
                "trusted sources",
                "vertrauenswürdige quellen",
                "vertrauenswuerdige quellen",
                "research findings",
                "research proposal",
                "rechercheergebnisse",
                "rechercheergebnis",
            ),
        )

    @staticmethod
    def _is_research_artifact_request(lowered: str) -> bool:
        """Detect research requests whose promised output is an artifact.

        Standalone research deliberately completes after bounded evidence is
        collected.  Once the user asks to write/save findings, however, a
        completion claim must also be backed by the requested file/report.
        Keep this check separate from filename parsing so ``research.txt`` in
        an ordinary write request remains a normal write goal.
        """
        research_intent = _has_research_intent(lowered)
        artifact_intent = _contains_word(
            lowered,
            (
                "write",
                "save",
                "store",
                "report",
                "findings",
                "proposal",
                "create a file",
                "schreibe",
                "speichere",
                "speichern",
                "bericht",
                "ergebnisse",
                "rechercheergebnis",
            ),
        )
        return research_intent and artifact_intent

    @staticmethod
    def _is_research_execution_request(lowered: str) -> bool:
        """Keep research-plus-execution requests out of research-only mode."""
        research_intent = _has_research_intent(lowered)
        execution_intent = _contains_word(
            lowered,
            (
                "implement",
                "implementiere",
                "implementieren",
                "build",
                "baue",
                "erstelle",
                "create",
                "entwickle",
                "entwickeln",
                "deploy",
                "install",
                "installiere",
                "ausführen",
                "ausfuehren",
                "execute",
                "umsetzen",
                "apply",
                "anwenden",
                "changes",
                "änderungen",
            ),
        )
        return research_intent and execution_intent

    @staticmethod
    def _is_install_compound_request(lowered: str) -> bool:
        """Route install-plus-deploy requests through bounded planning."""
        if not _starts_with(lowered, ("install ", "installiere ")):
            return False
        word_count = len(re.findall(r"(?<!\w)[\w]+(?!\w)", lowered))
        return word_count >= 6 and _contains_word(
            lowered,
            (
                "deploy",
                "deploye",
                "konfiguriere",
                "richte",
                "setup",
                "cluster",
                "vm",
                "sicheren",
                "secure",
            ),
        )

    @staticmethod
    def _is_mixed_read_write_request(lowered: str) -> bool:
        """Do not silently discard a requested output from a read summary."""
        read_intent = _contains_word(
            lowered,
            ("read", "lies", "zeige", "show", "summarize", "zusammenfasse"),
        )
        write_intent = _contains_word(
            lowered,
            (
                "write",
                "create",
                "erstelle",
                "erstellen",
                "schreibe",
                "save",
                "speichere",
                "report",
                "bericht",
                "summary",
                "zusammenfassung",
            ),
        )
        return read_intent and write_intent and _extract_path(lowered) is not None

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
                "migration",
                "datenbankmigration",
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
                "hardware passthrough",
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
                "operating system",
                "kernel",
                "hypervisor",
                "virtual machine",
                "virtualization",
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
                "migrate",
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
                "design",
                "develop",
                "perform",
                "architect",
                "execute",
                "run",
                "automate",
                "build",
                "create",
            ),
        )

    @staticmethod
    def _is_ambitious_unknown_request(lowered: str) -> bool:
        """Route unfamiliar multi-step goals to bounded research, not CLI rejection."""
        word_count = len(re.findall(r"(?<!\w)[\w]+(?!\w)", lowered))
        if word_count < 8:
            return False
        return _contains_word(
            lowered,
            (
                "build",
                "create",
                "design",
                "develop",
                "engineer",
                "construct",
                "architect",
                "assemble",
                "formulate",
                "establish",
                "implement",
                "configure",
                "deploy",
                "prepare",
                "redesign",
                "refactor",
                "rewrite",
                "rebuild",
                "port",
                "train",
                "evaluate",
                "solve",
                "analyze",
                "analyse",
                "optimize",
                "optimise",
                "integrate",
                "migrate",
                "prove",
                "verify",
                "plan",
                "plane",
                "entwickle",
                "entwickeln",
                "implementiere",
                "konfiguriere",
                "konfigurieren",
                "erstelle",
                "erstellen",
                "entwirf",
                "entwerfen",
                "analysiere",
                "analysieren",
                "untersuche",
                "untersuchen",
                "verifiziere",
                "verifizieren",
                "prüfe",
                "pruefe",
                "simuliere",
                "simulieren",
                "bewerte",
                "bewerten",
                "evaluieren",
                "führe",
                "fuehre",
                "set up",
                "richte",
                "richte ein",
            ),
        ) or (
            _contains_word(
                lowered,
                (
                    "i need",
                    "i want",
                    "can you",
                    "please",
                    "what would it take",
                    "ich brauche",
                    "ich möchte",
                    "kannst du",
                    "bitte",
                ),
            )
            and _contains_word(
                lowered,
                (
                    "system",
                    "service",
                    "stack",
                    "pipeline",
                    "engine",
                    "platform",
                    "compiler",
                    "virtual machine",
                    "database",
                    "security",
                    "sicherheit",
                    "distributed",
                    "verteilt",
                    "production",
                    "formal",
                    "hardware",
                    "recovery",
                    "wiederherstellung",
                    "encryption",
                    "verschlüsselung",
                ),
            )
        ) or (
            word_count >= 10
            and _contains_word(
                lowered,
                (
                    "system",
                    "service",
                    "stack",
                    "pipeline",
                    "engine",
                    "platform",
                    "compiler",
                    "virtual machine",
                    "database",
                    "security",
                    "sicherheit",
                    "distributed",
                    "verteilt",
                    "production",
                    "formal",
                    "hardware",
                    "recovery",
                    "wiederherstellung",
                    "encryption",
                    "verschlüsselung",
                ),
            )
        )

    @staticmethod
    def _is_natural_run_request(lowered: str) -> bool:
        return bool(
            re.search(
                r"(?:could\s+you|can\s+you|please|kannst\s+du|bitte)"
                r".*\b(?:run|execute|starte|starten|führe|fuehre|ausführen|ausfuehren)\b",
                lowered,
            )
        )

    @staticmethod
    def _is_placeholder_command(command: str) -> bool:
        return bool(
            re.fullmatch(
                r"(?:a|an|the|ein|eine|der|die|das)\s+"
                r"(?:(?:shell|bash)\s+)?(?:script|skript|command|kommando)",
                command.strip(),
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _looks_like_explicit_command(goal: str) -> bool:
        """Keep verification wording from reclassifying a real CLI command."""
        command = _after_command_verb(goal)
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            return False
        if not argv:
            return False
        executable = Path(argv[0]).name.casefold()
        return executable in {
            "bash",
            "cat",
            "echo",
            "git",
            "ls",
            "mypy",
            "node",
            "npm",
            "printf",
            "python",
            "python3",
            "pytest",
            "qemu-system-x86_64",
            "ruff",
            "sh",
            "uv",
        } or "/" in argv[0] or any(item.startswith("-") for item in argv[1:])

    def _research_task(
        self, goal: str, *, research_only: bool = False
    ) -> NormalizedGoal:
        criteria = [
            AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
            AcceptanceCriterion("research", CriterionKind.RESEARCHED),
        ]
        artifact = _extract_path(goal) if not research_only else None
        if artifact is not None and self._is_research_artifact_request(goal.casefold()):
            criteria.append(AcceptanceCriterion("artifact", CriterionKind.FILE_EXISTS, artifact))
        criteria.append(AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED))
        return _goal(
            goal,
            GoalKind.RESEARCH_TASK,
            "Research and bound an unfamiliar complex task before execution",
            target="research-only" if research_only else "complex-task",
            implicit_requirements=(
                "Research uses the local capability matrix and trusted background sources without opening a browser.",
                "Do not claim execution until an implementation plan and independent E2E evidence exist.",
            ),
            criteria=tuple(criteria),
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

    def _security_scan(self, goal: str) -> NormalizedGoal:
        return _goal(
            goal,
            GoalKind.SECURITY_SCAN,
            "Bounded project security scan",
            target=".",
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("findings", CriterionKind.OUTPUT_PRODUCED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _host_security_scan(self, goal: str) -> NormalizedGoal:
        return _goal(
            goal,
            GoalKind.HOST_SECURITY_SCAN,
            "Bounded host and network security check",
            target="host",
            criteria=(
                AcceptanceCriterion("action", CriterionKind.ACTION_SUCCEEDED),
                AcceptanceCriterion("findings", CriterionKind.OUTPUT_PRODUCED),
                AcceptanceCriterion("e2e", CriterionKind.E2E_VERIFIED),
            ),
        )

    def _run(self, goal: str) -> NormalizedGoal:
        return self._run_command(goal, _after_command_verb(goal))

    def _run_natural(self, goal: str) -> NormalizedGoal:
        before_verb = re.search(
            r"(?:kannst\s+du|can\s+you|please|bitte)\s+"
            r"(?:bitte\s+)?(.+?)\s+"
            r"(?:ausführen|ausfuehren|starten)\b",
            goal,
            flags=re.IGNORECASE,
        )
        match = re.search(
            r"\b(?:run|execute|starte|starten|führe|fuehre)\b\s+(.+)",
            goal,
            flags=re.IGNORECASE,
        )
        if before_verb is not None:
            command = before_verb.group(1)
        elif match is not None:
            command = match.group(1)
        else:
            raise GoalError("natural run goal requires a command")
        command = re.split(
            r"\s+(?:and|und)\s+(?:verify|prüfe|pruefe|teste|test|das ergebnis)\b",
            command,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        if self._is_placeholder_command(command):
            raise GoalError("natural run goal requires a concrete executable")
        return self._run_command(goal, command)

    def _run_command(self, goal: str, command: str) -> NormalizedGoal:
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


def _has_research_intent(value: str) -> bool:
    """Recognize research language without treating ``research.txt`` as it."""
    return bool(
        re.search(
            r"(?<![\w.])(?:research|recherche|recherchiere|recherchieren|"
            r"hintergrundrecherche)(?![\w.])",
            value,
        )
    ) or _contains_word(
        value,
        ("trusted sources", "vertrauenswürdige quellen", "vertrauenswuerdige quellen"),
    )


def _extract_path(goal: str) -> str | None:
    # Prefer an explicit filename token before considering a generic fenced
    # value, otherwise `Erstelle result.txt mit dem Inhalt `value`` would use
    # the content as the path.
    candidates = [match.group(2) for match in _PATH_TOKEN.finditer(goal)]
    backtick = re.search(r"`([^`]+)`", goal)
    if backtick:
        candidates.append(backtick.group(1))
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
