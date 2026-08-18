from pathlib import Path


class ExecutionEngine:

    def execute(self, step):

        if hasattr(step, "action"):
            action = step.action
            target = step.target

        elif isinstance(step, dict):
            action = step.get("action")
            target = step.get("target")

        else:
            return {
                "success": False,
                "error": "unknown step format"
            }


        if action == "create_file":

            path = Path(target)

            path.write_text(
                "Created by Autonomous Agent V50.6.2\n",
                encoding="utf-8"
            )

            return {
                "success": True,
                "action": "create_file",
                "file": str(path)
            }


        return {
            "success": False,
            "action": action,
            "message": "action not implemented"
        }
