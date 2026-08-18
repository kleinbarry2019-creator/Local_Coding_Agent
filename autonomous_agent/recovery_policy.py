from datetime import datetime


class RecoveryPolicyEngine:

    MAX_RECOVERY_ATTEMPTS = 3


    def __init__(self, state_manager):

        self.state_manager = state_manager


    def evaluate(self):

        state = self.state_manager.state


        if not state:
            return {
                "action": "initialize",
                "reason": "no_state"
            }


        attempts = state.get(
            "recovery_attempts",
            0
        )


        if attempts >= self.MAX_RECOVERY_ATTEMPTS:

            return {
                "action": "safe_mode",
                "reason": "too_many_recoveries"
            }


        if state.get("recovery_event"):

            return {
                "action": "verify",
                "reason": "previous_recovery_detected"
            }


        return {
            "action": "continue",
            "reason": "state_healthy"
        }


    def execute(self):

        decision = self.evaluate()


        if decision["action"] == "safe_mode":

            self.state_manager.state["status"] = "safe_mode"

            self.state_manager.save(
                snapshot=False
            )


        elif decision["action"] == "verify":

            self.state_manager.verify_snapshot_chain()


        self.state_manager.write_audit_event(
            {
                "event": "recovery_policy",
                "decision": decision,
                "timestamp": datetime.utcnow().isoformat()
            }
        )


        return decision
