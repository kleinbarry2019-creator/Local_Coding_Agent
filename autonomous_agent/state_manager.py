import json
import hashlib
import uuid
import os
from datetime import datetime


class StateManager:

    SCHEMA_VERSION = 1

    def __init__(self, path="agent_state.json"):
        self.path = path
        self.state = {}
        self.history_path = "agent_history.json"
        self.audit_path = "agent_audit.json"
        self.recovery_audit_enabled = True
        self.recovered = False
        self.history_path = "agent_history.json"
        self.audit_path = "agent_audit.json"
        self.load()


    def migrate_state(self, state):

        if not isinstance(state, dict):
            return {}

        version = state.get("schema_version", 0)

        if version < 1:
            state["schema_version"] = 1

        return state


    def load(self):

        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.state = self.migrate_state(json.load(f))

            except Exception:
                self.state = {}

        else:
            self.state = {}

        return self.state




    def write_audit_event(self, event):

        events = []

        if os.path.exists(self.audit_path):
            try:
                with open(self.audit_path, "r", encoding="utf-8") as f:
                    events = json.load(f)
            except Exception:
                events = []

        events.append(event)

        with open(self.audit_path, "w", encoding="utf-8") as f:
            json.dump(
                events,
                f,
                indent=2
            )

    def save_snapshot(self, label="checkpoint"):

        snapshot_state = self.serialize(self.state)

        history = []

        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

        previous_hash = ""

        if history:
            previous_hash = history[-1].get("hash", "")

        timestamp = datetime.utcnow().isoformat()

        snapshot = {
            "id": str(uuid.uuid4()),
            "label": label,
            "timestamp": timestamp,
            "previous_hash": previous_hash,
            "hash": self.snapshot_chain_hash(
                snapshot_state,
                previous_hash,
                label,
                timestamp
            ),
            "state": snapshot_state
        }

        history.append(snapshot)

        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(
                history,
                f,
                indent=2
            )

        return snapshot



    def verify_snapshot_chain(self):

        if not os.path.exists(self.history_path):
            return False

        with open(self.history_path, "r", encoding="utf-8") as f:
            history = json.load(f)

        previous = ""

        for item in history:
            expected = self.snapshot_chain_hash(
                item["state"],
                previous,
                item["label"],
                item["timestamp"]
            )

            if item.get("hash") != expected:
                return False

            if item.get("previous_hash", "") != previous:
                return False

            previous = item["hash"]

        return True


    def find_last_valid_snapshot(self):

        if not os.path.exists(self.history_path):
            return None

        with open(self.history_path, "r", encoding="utf-8") as f:
            history = json.load(f)

        previous = ""

        valid = None

        for item in history:

            try:
                expected = self.snapshot_chain_hash(
                    item["state"],
                    previous,
                    item["label"],
                    item["timestamp"]
                )

                if (
                    item.get("hash") != expected or
                    item.get("previous_hash", "") != previous
                ):
                    break

                valid = item
                previous = item["hash"]

            except Exception:
                break

        return valid


    def restore_safe_point(self):

        snapshot = self.find_last_valid_snapshot()

        if not snapshot:
            return False

        self.state = snapshot["state"]

        self.recovered = True

        self.save(snapshot=False)

        self.write_audit_event({
            "event": "state_recovery",
            "restored_snapshot": snapshot.get("label"),
            "reason": "hash mismatch",
            "timestamp": datetime.utcnow().isoformat(),
            "severity": "warning"
        })

        return True

    def restore_snapshot(self, snapshot_id):

        if not self.verify_snapshot_chain():
            return False

        if not os.path.exists(self.history_path):
            return False

        with open(self.history_path, "r", encoding="utf-8") as f:
            history = json.load(f)

        for item in reversed(history):
            if item.get("id") == snapshot_id:

                expected = item.get("hash")

                if expected:
                    actual = self.snapshot_hash(item["state"])

                    if actual != expected:
                        return False

                self.state = item["state"]

                # restored state persistent speichern
                self.save(snapshot=False)

                return True

        return False




    def snapshot_chain_hash(self, snapshot_state, previous_hash, label, timestamp):
        payload = json.dumps(
            {
                "state": self.serialize(snapshot_state),
                "previous_hash": previous_hash,
                "label": label,
                "timestamp": timestamp
            },
            sort_keys=True
        ).encode("utf-8")

        return hashlib.sha256(payload).hexdigest()

    def snapshot_hash(self, state):
        payload = json.dumps(
            self.serialize(state),
            sort_keys=True
        ).encode("utf-8")

        return hashlib.sha256(payload).hexdigest()

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


    def save(self, data=None, snapshot=True):

        if data is not None:
            if isinstance(data, dict):
                self.state.update(data)
            else:
                self.state["result"] = data

        self.state["schema_version"] = self.SCHEMA_VERSION
        self.state["updated"] = datetime.now().isoformat()

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                self.serialize(self.state),
                f,
                indent=2,
                ensure_ascii=False
            )

        if snapshot:
            self.save_snapshot("auto-save")

    def __setitem__(self, key, value):
        self.state[key] = value
        self.save(snapshot=False)


    def __repr__(self):
        return str(self.state)
