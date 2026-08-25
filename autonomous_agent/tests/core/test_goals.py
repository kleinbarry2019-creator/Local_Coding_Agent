from __future__ import annotations

import pytest

from autonomous_agent.core.goals import (
    CriterionKind,
    GoalError,
    GoalKind,
    GoalNormalizer,
)


@pytest.mark.parametrize(
    ("goal_text", "kind", "target"),
    [
        ("Erstelle `notes.txt` mit Inhalt Hallo", GoalKind.WRITE_FILE, "notes.txt"),
        ("Write report.md with content ready", GoalKind.WRITE_FILE, "report.md"),
        ("Lies `README.md`", GoalKind.READ_FILE, "README.md"),
        ("Liste Dateien in `docs`", GoalKind.LIST_FILES, "docs"),
        ("Analysiere das Projekt und seine Sprachen", GoalKind.ANALYZE_PROJECT, "."),
        ("Installiere ruff", GoalKind.INSTALL_TOOL, "ruff"),
        (
            "Baue mir eine Windows VM mit Zugriff auf CPU, GPU und Speicher",
            GoalKind.VM_BUILD,
            "windows-vm",
        ),
        (
            "Implementiere eine Android-App mit Offline-Synchronisierung",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Führe einen umfassenden Sicherheits-Audit mit Bedrohungsmodell durch",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Entwickle ein Machine-Learning-Modell zur Anomalieerkennung",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Konzipiere und implementiere eine fehlertolerante verteilte Datenpipeline",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Entwickle einen Compiler mit Lexer, Parser und Bytecode-VM",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Baue eine multimodale 3D-Anwendung mit Echtzeit-Rendering",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Migriere die bestehende Datenbank sicher auf ein versioniertes Schema",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Plane und setze eine Zero-Trust-Netzwerkarchitektur mit mTLS und Policy-as-Code um",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Führe eine vollständige Web-Sicherheitsanalyse mit DAST und Threat Modeling durch",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Entwickle einen sicheren Betriebssystemkern mit Scheduler und Sandboxing",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Automatisiere eine containerisierte Multi-Cluster-Infrastruktur mit Service Mesh",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Implementiere eine wissenschaftliche GPU-Simulation mit numerischer Validierung",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Design and implement a secure operating system kernel with memory management",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Build a hardware-assisted hypervisor with VM isolation and recovery tests",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Migrate the production database to a versioned schema with rollback",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Perform a complete security audit with threat modeling and exploit verification",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Recherchiere autonom im Hintergrund nach vertrauenswürdigen Quellen",
            GoalKind.RESEARCH_TASK,
            "research-only",
        ),
        (
            "Research trusted sources for secure AI practices without opening a browser",
            GoalKind.RESEARCH_TASK,
            "research-only",
        ),
        (
            "Train and evaluate a reinforcement learning agent in a simulated robotics environment with reproducible benchmarks",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Create a 3D CAD model of a turbine with finite-element stress analysis and manufacturing drawings",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Port this application to WebAssembly and optimize it with SIMD while preserving behavior across browsers",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Plane autonome Forschung zu neuen KI-Sicherheitsverfahren und implementiere die verifizierten Verbesserungen",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Führe eine forensische Analyse eines unbekannten Speicherdumps durch und erstelle einen beweissicheren Bericht",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Engineer a fault-tolerant satellite communication protocol with formal verification, radiation testing, and key rotation",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Construct a market-risk engine with Monte Carlo simulation, stress scenarios, explainable reports, and audit trails",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Redesign the storage layer for erasure coding, online repair, snapshots, and disaster recovery",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Verifiziere eine sicherheitskritische Steuerungssoftware formal und liefere reproduzierbare Beweise",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "I need a fault-tolerant satellite communication stack with formal verification, radiation testing, and secure key rotation",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "Please prepare a Windows virtual machine with hardware passthrough, snapshots, rollback, and crash recovery",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
        (
            "A production-grade secrets management service with HSM integration, rotation, disaster recovery, and compliance evidence",
            GoalKind.RESEARCH_TASK,
            "complex-task",
        ),
    ],
)
def test_normalizes_simple_german_and_english_requests(
    goal_text: str, kind: GoalKind, target: str
) -> None:
    goal = GoalNormalizer().normalize(goal_text)

    assert goal.kind is kind
    assert goal.target == target
    assert goal.implicit_requirements
    assert goal.acceptance_criteria[-1].kind is CriterionKind.E2E_VERIFIED


def test_command_is_tokenized_without_a_shell() -> None:
    goal = GoalNormalizer().normalize("Run python3 -c 'print(42)'")

    assert goal.kind is GoalKind.RUN_COMMAND
    assert goal.argv == ("python3", "-c", "print(42)")
    assert CriterionKind.COMMAND_EXITED_ZERO in {
        item.kind for item in goal.acceptance_criteria
    }


def test_polite_run_request_extracts_command_before_verification_text() -> None:
    goal = GoalNormalizer().normalize(
        "Could you run python3 -c 'print(42)' and verify the result"
    )

    assert goal.kind is GoalKind.RUN_COMMAND
    assert goal.argv == ("python3", "-c", "print(42)")


@pytest.mark.parametrize(
    "goal_text",
    [
        "Kannst du bitte python3 -c 'print(42)' ausführen und das Ergebnis prüfen?",
        "Kannst du python3 -c 'print(42)' starten und danach testen",
    ],
)
def test_german_polite_run_variants_extract_command(goal_text: str) -> None:
    goal = GoalNormalizer().normalize(goal_text)

    assert goal.kind is GoalKind.RUN_COMMAND
    assert goal.argv == ("python3", "-c", "print(42)")


def test_german_create_variant_remains_a_write_goal() -> None:
    goal = GoalNormalizer().normalize("Kannst du bitte `notes.txt` erstellen?")

    assert goal.kind is GoalKind.WRITE_FILE
    assert goal.target == "notes.txt"


@pytest.mark.parametrize(
    "goal_text",
    [
        "Research trusted sources and write the findings to research.txt",
        "Recherchiere vertrauenswürdige Quellen und speichere die Ergebnisse in findings.md",
    ],
)
def test_research_intent_has_priority_over_report_filename(goal_text: str) -> None:
    goal = GoalNormalizer().normalize(goal_text)

    assert goal.kind is GoalKind.RESEARCH_TASK
    assert goal.target == "research-only"


def test_mixed_repository_analysis_and_report_is_not_silently_only_a_write() -> None:
    goal = GoalNormalizer().normalize(
        "Analyze the repository and write a language report to languages.md"
    )

    assert goal.kind is GoalKind.RESEARCH_TASK
    assert goal.target == "complex-task"


def test_complex_goal_with_report_target_is_not_silently_an_empty_write() -> None:
    goal = GoalNormalizer().normalize(
        "Build a distributed event platform and write a complete implementation plan to architecture.md"
    )

    assert goal.kind is GoalKind.RESEARCH_TASK
    assert goal.target == "complex-task"


def test_write_derives_content_acceptance_criterion() -> None:
    goal = GoalNormalizer().normalize(
        "Erstelle `result.txt` mit dem Inhalt `verified`"
    )

    criterion = next(
        item
        for item in goal.acceptance_criteria
        if item.kind is CriterionKind.FILE_CONTENT_EQUALS
    )
    assert goal.content == "verified"
    assert criterion.target == "result.txt"
    assert criterion.expected == "verified"


def test_research_word_in_filename_does_not_override_write_intent() -> None:
    goal = GoalNormalizer().normalize("Erstelle research.txt mit dem Inhalt verified")

    assert goal.kind is GoalKind.WRITE_FILE
    assert goal.target == "research.txt"
    assert goal.content == "verified"


@pytest.mark.parametrize(
    "goal_text",
    ["", "do something clever", "read ../../etc/passwd", "install ruff extra"],
)
def test_ambiguous_or_unsafe_goals_fail_closed(goal_text: str) -> None:
    with pytest.raises(GoalError):
        GoalNormalizer().normalize(goal_text)
