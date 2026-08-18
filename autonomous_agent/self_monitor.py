import os
import json
import hashlib
from datetime import datetime


class SelfMonitor:
    def __init__(
        self,
        state_file="autonomous_agent/agent_state.json",
    ):
        self.state_file = state_file


    def now(self):
        return datetime.utcnow().isoformat()


    def check_file(self, path):
        return os.path.exists(path)


    def checksum(self, path):
        if not os.path.exists(path):
            return None

        sha256 = hashlib.sha256()

        with open(
            path,
            "rb",
        ) as f:
            for block in iter(
                lambda: f.read(4096),
                b"",
            ):
                sha256.update(block)

        return sha256.hexdigest()


    def check_memory(self):
        result = {
            "component": "memory",
            "healthy": False,
            "time": self.now(),
        }

        if not self.check_file(
            self.state_file
        ):
            result["error"] = "memory missing"
            return result

        try:
            with open(
                self.state_file,
                "r",
                encoding="utf-8",
            ) as f:
                json.load(f)

            result["healthy"] = True

        except Exception as e:
            result["error"] = str(e)

        return result


    def system_report(self):
        return {
            "time": self.now(),
            "checks": [
                self.check_memory(),
            ],
        }
