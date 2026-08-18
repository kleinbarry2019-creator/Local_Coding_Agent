import json
import os
from datetime import datetime


class MemoryManager:
    def __init__(
        self,
        state_file="autonomous_agent/agent_state.json",
    ):
        self.state_file = state_file

        self.state = {
            "created": self._now(),
            "updated": self._now(),
            "goals": [],
            "plans": [],
            "executions": [],
            "errors": [],
            "last_status": "idle",
        }

        self.load()


    def _now(self):
        return datetime.utcnow().isoformat()


    def load(self):
        if not os.path.exists(self.state_file):
            return

        try:
            with open(
                self.state_file,
                "r",
                encoding="utf-8",
            ) as f:
                self.state = json.load(f)

        except Exception:
            self.state["errors"].append(
                {
                    "time": self._now(),
                    "type": "memory_load_error",
                }
            )


    def save(self):
        self.state["updated"] = self._now()

        os.makedirs(
            os.path.dirname(self.state_file),
            exist_ok=True,
        )

        with open(
            self.state_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                self.state,
                f,
                indent=2,
                ensure_ascii=False,
            )


    def add_goal(self, goal):
        self.state["goals"].append(
            {
                "goal": goal,
                "time": self._now(),
            }
        )

        self.save()


    def add_plan(self, plan):
        self.state["plans"].append(
            {
                "plan": plan,
                "time": self._now(),
            }
        )

        self.save()


    def add_execution(
        self,
        step,
        result,
    ):
        self.state["executions"].append(
            {
                "step": step,
                "result": result,
                "time": self._now(),
            }
        )

        self.save()


    def add_error(
        self,
        error,
    ):
        self.state["errors"].append(
            {
                "error": str(error),
                "time": self._now(),
            }
        )

        self.save()


    def set_status(
        self,
        status,
    ):
        self.state["last_status"] = status
        self.save()


    def get_state(self):
        return self.state
