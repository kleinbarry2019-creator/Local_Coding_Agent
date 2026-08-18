from datetime import datetime
import importlib


class AgentValidator:

    def __init__(self):
        self.results = []


    def check_module(self, module_name):

        try:
            importlib.import_module(module_name)

            self.results.append({
                "module": module_name,
                "healthy": True
            })

        except Exception as error:

            self.results.append({
                "module": module_name,
                "healthy": False,
                "error": str(error)
            })


    def validate(self):

        modules = [
            "autonomous_agent.orchestrator",
            "autonomous_agent.planner",
            "autonomous_agent.execution_engine",
            "autonomous_agent.memory_manager",
            "autonomous_agent.self_monitor",
            "autonomous_agent.self_healer",
        ]

        for module in modules:
            self.check_module(module)


        return {
            "time": datetime.now().isoformat(),
            "healthy": all(
                item["healthy"]
                for item in self.results
            ),
            "checks": self.results
        }
