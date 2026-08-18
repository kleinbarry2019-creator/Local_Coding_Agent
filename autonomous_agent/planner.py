from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PlanStep:
    action: str
    target: Optional[str] = None
    reason: str = ""


class Planner:
    """
    Erstellt Ausführungspläne für den Agenten.
    Später durch LLM-Planung ersetzbar.
    """

    def create_plan(self, goal):

        if not goal:
            return []

        if isinstance(goal, dict):

            if goal.get("goal") == "create test file":

                return [
                    PlanStep(
                        action="create_file",
                        target=goal.get("target"),
                        reason="Erzeuge Testdatei"
                    )
                ]

        return [
            PlanStep(
                action="analyze",
                reason="Analysiere Benutzerziel"
            ),
            PlanStep(
                action="execute",
                reason="Führe notwendigen Schritt aus"
            ),
            PlanStep(
                action="verify",
                reason="Prüfe Ergebnis"
            )
        ]
