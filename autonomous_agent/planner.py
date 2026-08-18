from dataclasses import dataclass
from typing import List


@dataclass
class PlanStep:
    action: str
    tool: str | None = None
    reason: str = ""


class Planner:
    """
    Erstellt eine einfache Ausführungsplanung.
    Noch deterministisch, später durch LLM ersetzbar.
    """

    def create_plan(self, goal: str) -> List[PlanStep]:

        if not goal:
            return []

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
            ),
        ]
