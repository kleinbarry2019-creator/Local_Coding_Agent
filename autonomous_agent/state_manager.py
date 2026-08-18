import json
import os
from datetime import datetime


class StateManager:

    def __init__(self, path="agent_state.json"):
        self.path = path
        self.state = {}
        self.load()


    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self.state = json.load(f)
            except Exception:
                self.state = {}
        else:
            self.state = {}


    def serialize(self, obj, seen=None):

        if seen is None:
            seen = set()

        if obj is self:
            return "<state_manager>"

        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj

        obj_id = id(obj)

        if obj_id in seen:
            return "<recursive>"

        seen.add(obj_id)

        if isinstance(obj, list):
            return [
                self.serialize(x, seen)
                for x in obj
                if x is not self
            ]

        if isinstance(obj, dict):
            return {
                str(k): self.serialize(v, seen)
                for k, v in obj.items()
                if v is not self
            }

        if hasattr(obj, "__dict__"):

            result = {}

            for key, value in obj.__dict__.items():

                if key == "state":
                    continue

                if value is self:
                    result[key] = "<state_manager>"
                else:
                    result[key] = self.serialize(value, seen)

            return result

        return str(obj)

    def __getitem__(self, key):
        return self.state.get(key)


    def save(self, data=None):

        if data is not None:
            if isinstance(data, dict):
                self.state.update(data)
            else:
                self.state["result"] = data

        self.state["updated"] = datetime.now().isoformat()

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                self.serialize(self.state),
                f,
                indent=2,
                ensure_ascii=False
            )

    def __setitem__(self, key, value):
        self.state[key] = value
        self.save()


    def __repr__(self):
        return str(self.state)
