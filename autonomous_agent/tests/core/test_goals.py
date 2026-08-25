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


@pytest.mark.parametrize(
    "goal_text",
    ["", "do something clever", "read ../../etc/passwd", "install ruff extra"],
)
def test_ambiguous_or_unsafe_goals_fail_closed(goal_text: str) -> None:
    with pytest.raises(GoalError):
        GoalNormalizer().normalize(goal_text)
