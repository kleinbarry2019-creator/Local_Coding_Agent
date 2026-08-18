from pathlib import Path
import importlib

from autonomous_agent.tool_registry import ToolRegistry


class ToolBootstrap:
    """
    Lädt automatisch alle verfügbaren Tools
    und registriert sie im zentralen Registry-System.
    """

    def __init__(self, tools_path=None):
        self.registry = ToolRegistry()

        if tools_path is None:
            self.tools_path = (
                Path(__file__).parent / "tools"
            )
        else:
            self.tools_path = Path(tools_path)


    def discover_tools(self):
        """
        Findet alle Python-Toolmodule.
        """

        tools = []

        for file in self.tools_path.glob("*.py"):

            if file.name.startswith("_"):
                continue

            tools.append(
                file.stem
            )

        return tools


    def load_tools(self):
        """
        Importiert gefundene Tools.
        """

        loaded = []

        for tool_name in self.discover_tools():

            module_name = (
                f"autonomous_agent.tools.{tool_name}"
            )

            try:
                module = importlib.import_module(
                    module_name
                )

                if hasattr(
                    module,
                    "register_tools"
                ):
                    module.register_tools(
                        self.registry
                    )

                    loaded.append(
                        tool_name
                    )

            except Exception as error:

                print(
                    f"[TOOL LOAD ERROR] {tool_name}: {error}"
                )

        return loaded


    def get_registry(self):
        return self.registry



def bootstrap():

    loader = ToolBootstrap()

    loaded = loader.load_tools()

    print(
        f"Loaded tools: {loaded}"
    )

    return loader.registry



if __name__ == "__main__":

    registry = bootstrap()

    print(
        registry.list_tools()
    )
