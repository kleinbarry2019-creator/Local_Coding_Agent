from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class ToolDefinition:
    name: str
    description: str
    risk: str
    handler: Callable
    max_calls: int = 5


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(
        self,
        name: str,
        description: str,
        risk: str,
        handler: Callable,
        max_calls: int = 5,
    ):
        if name in self.tools:
            raise RuntimeError(
                f"Tool existiert bereits: {name}"
            )

        self.tools[name] = ToolDefinition(
            name=name,
            description=description,
            risk=risk,
            handler=handler,
            max_calls=max_calls,
        )

    def get(self, name: str):
        if name not in self.tools:
            raise RuntimeError(
                f"Unbekanntes Tool: {name}"
            )

        return self.tools[name]

    def list_tools(self):
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": tool.risk,
                "max_calls": tool.max_calls,
            }
            for tool in self.tools.values()
        ]

    def execute(
        self,
        name: str,
        *args,
        **kwargs,
    ) -> Any:

        tool = self.get(name)

        return tool.handler(
            *args,
            **kwargs,
        )
