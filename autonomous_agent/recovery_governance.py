from datetime import datetime


class RecoveryGovernanceEngine:

    AUTO_THRESHOLD = 0.85
    VERIFY_THRESHOLD = 0.50


    def __init__(self, state_manager):

        self.state_manager = state_manager


    def calculate_confidence(self):

        score = 1.0
        state = self.state_manager.state


        if not state:
            score -= 0.0


        attempts = state.get(
            "recovery_attempts",
            0
        )

        score -= min(
            attempts * 0.15,
            0.45
        )


        if state.get("recovery_event"):
            score -= 0.1


        if state.get("recovery_audit_chain"):

            try:
                if not self.state_manager.verify_snapshot_chain():
                    score -= 0.15

            except Exception:
                score -= 0.4


        return max(
            0.0,
            round(score, 2)
        )


    def authorize(self, decision):

        confidence = self.calculate_confidence()


        if confidence >= self.AUTO_THRESHOLD:

            action = "AUTO"


        elif confidence >= self.VERIFY_THRESHOLD:

            action = "VERIFY"


        else:

            action = "SAFE_MODE"


        event = {
            "event": "recovery_governance",
            "policy_decision": decision,
            "confidence": confidence,
            "authorization": action,
            "timestamp": datetime.utcnow().isoformat()
        }


        self.state_manager.write_audit_event(event)


        return {
            "authorization": action,
            "confidence": confidence
        }
