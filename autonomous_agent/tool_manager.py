from autonomous_agent.tool_registry import ToolRegistry
from autonomous_agent.tool_loader import ToolLoader


class ToolManager:

    def __init__(self):
        self.registry = ToolRegistry()
        self.loader = ToolLoader()

    def discover(self, path="autonomous_agent/tools"):
        return self.loader.load_tools(
            path,
            self.registry
        )

    def capabilities(self):
        return self.registry.list_tools()

    def run(self, name, *args, **kwargs):
        return self.registry.execute(
            name,
            *args,
            **kwargs
        )

    def health(self):
        return {
            "tools_loaded": len(
                self.registry.tools
            ),
            "status": "ready"
        }
