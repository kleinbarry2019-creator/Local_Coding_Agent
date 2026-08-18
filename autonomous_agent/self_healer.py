import os
import json
import shutil
from datetime import datetime


class SelfHealer:

    def __init__(
        self,
        state_file="autonomous_agent/agent_state.json",
        backup_file="autonomous_agent/agent_state.json.bak",
    ):
        self.state_file = state_file
        self.backup_file = backup_file


    def now(self):
        return datetime.utcnow().isoformat()


    def create_backup(self):
        if os.path.exists(self.state_file):
            shutil.copy2(
                self.state_file,
                self.backup_file,
            )

            return True

        return False


    def restore_backup(self):
        if not os.path.exists(self.backup_file):
            return False

        shutil.copy2(
            self.backup_file,
            self.state_file,
        )

        return True


    def validate_state(self):

        if not os.path.exists(
            self.state_file
        ):
            return False

        try:
            with open(
                self.state_file,
                "r",
                encoding="utf-8",
            ) as f:
                json.load(f)

            return True

        except Exception:
            return False


    def repair(self):

        result = {
            "time": self.now(),
            "repaired": False,
            "action": None,
        }


        if self.validate_state():
            result["action"] = "state healthy"
            return result


        if self.restore_backup():
            result["repaired"] = True
            result["action"] = "backup restored"
            return result


        result["action"] = "repair failed"

        return result
