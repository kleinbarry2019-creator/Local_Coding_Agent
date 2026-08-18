import importlib
import pkgutil
import traceback


class ToolLoader:
    def __init__(self, registry):
        self.registry = registry
        self.loaded = []


    def load_tools(self):
        package_name = "autonomous_agent.tools"

        package = importlib.import_module(
            package_name
        )

        for module_info in pkgutil.iter_modules(
            package.__path__
        ):

            name = module_info.name

            if not name.endswith("_tools"):
                continue

            full_name = f"{package_name}.{name}"

            try:
                module = importlib.import_module(
                    full_name
                )

                register_function = getattr(
                    module,
                    "register_tools",
                    None,
                )

                if register_function:
                    register_function(
                        self.registry
                    )

                self.loaded.append(
                    full_name
                )

            except Exception:
                print(
                    f"Tool konnte nicht geladen werden: {full_name}"
                )

                traceback.print_exc()

        return self.loaded
