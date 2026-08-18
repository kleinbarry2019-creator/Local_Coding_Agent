import json
import hashlib
from datetime import datetime


class RecoveryAuditChain:


    def __init__(self, state_manager):

        self.state_manager = state_manager



    def _canonical(self, payload):

        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":")
        )



    def _hash(self, payload):

        return hashlib.sha256(
            self._canonical(payload).encode()
        ).hexdigest()



    def append_event(self, event):

        state = self.state_manager.state

        chain = state.setdefault(
            "recovery_audit_chain",
            []
        )


        previous_hash = (
            chain[-1]["hash"]
            if chain
            else "GENESIS"
        )


        timestamp = datetime.utcnow().isoformat()


        payload = {
            "timestamp": timestamp,
            "previous_hash": previous_hash,
            "event": event
        }


        record = payload.copy()

        record["hash"] = self._hash(
            payload
        )


        chain.append(
            record
        )


        self.state_manager.save(
            snapshot=False
        )


        return record



    def verify(self):

        chain = self.state_manager.state.get(
            "recovery_audit_chain",
            []
        )


        previous_hash = "GENESIS"


        for record in chain:

            payload = {
                "timestamp": record["timestamp"],
                "previous_hash": record["previous_hash"],
                "event": record["event"]
            }


            expected = self._hash(
                payload
            )


            if record["previous_hash"] != previous_hash:
                return False


            if record["hash"] != expected:
                return False


            previous_hash = record["hash"]


        return True
