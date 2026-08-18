from dataclasses import dataclass
from typing import Any

from .tool_manager import ToolManager


@dataclass
class AgentTask:
    goal: str
    completed: bool = False
    result: Any = None


class AgentLoop:

    def __init__(self, tool_manager: ToolManager):
        self.tool_manager = tool_manager
        self.history = []

    def observe(self, event):
        self.history.append(event)

    def decide(self, task: AgentTask):
        """
        Minimaler Entscheidungsplatzhalter.
        Später:
        - LLM Planung
        - Prioritäten
        - Selbstkorrektur
        """

        if task.goal.startswith("list"):
            return {
                "tool": "filesystem.list",
                "args": {}
            }

        return None


    def execute_step(self, task: AgentTask):

        action = self.decide(task)

        if action is None:
            task.completed = True
            task.result = "Keine Aktion notwendig"
            return task


        result = self.tool_manager.execute(
            action["tool"],
            **action["args"]
        )

        self.observe({
            "action": action,
            "result": result
        })

        task.result = result

        return task


    def run(self, goal: str):

        task = AgentTask(goal)

        while not task.completed:
            self.execute_step(task)

        return task
