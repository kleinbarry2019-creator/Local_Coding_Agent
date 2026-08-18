from datetime import datetime
import importlib


class Validator:

    def __init__(self):
        self.modules = [
            "autonomous_agent.orchestrator",
            "autonomous_agent.planner",
            "autonomous_agent.execution_engine",
            "autonomous_agent.memory_manager",
            "autonomous_agent.self_monitor",
            "autonomous_agent.self_healer",
            "autonomous_agent.state_manager",
        ]


    def check(self):

        checks = []

        for module in self.modules:
            try:
                importlib.import_module(module)

                checks.append({
                    "module": module,
                    "healthy": True
                })

            except Exception as e:

                checks.append({
                    "module": module,
                    "healthy": False,
                    "error": str(e)
                })


        return {
            "time": datetime.now().isoformat(),
            "healthy": all(
                item["healthy"] for item in checks
            ),
            "checks": checks
        }
