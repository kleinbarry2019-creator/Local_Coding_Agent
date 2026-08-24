from datetime import datetime


class RecoveryAuthority:

    def __init__(
        self,
        state_manager,
        decision,
        recovery_allowed
    ):
        self.state_manager = state_manager
        self.decision = decision
        self.recovery_allowed = recovery_allowed


    def evaluate(self):

        state = self.state_manager.state

        confidence = float(
            self.state_manager.state.get(
                "recovery_confidence",
                0.0
            )
        )

        attempts = state.get(
            "recovery_attempts",
            0
        )

        if attempts >= 3:
            result = {
                "authority": "HUMAN_REQUIRED",
                "execution_blocked": True,
                "reason": "too_many_recovery_attempts"
            }

        elif confidence >= 0.85 and self.recovery_allowed:

            result = {
                "authority": "AUTO_RECOVERY",
                "execution_blocked": False,
                "reason": "high_confidence"
            }

        elif confidence >= 0.50:

            result = {
                "authority": "VERIFY",
                "execution_blocked": not self.recovery_allowed,
                "reason": "verification_required"
            }

        else:

            result = {
                "authority": "SAFE_MODE",
                "execution_blocked": True,
                "reason": "low_confidence"
            }


        self.record(result)

        return result


    def record(self, result):

        state = self.state_manager.state

        history = state.setdefault(
            "recovery_history",
            []
        )

        history.append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "decision": self.decision,
                "result": result
            }
        )

        state["execution_blocked"] = result["execution_blocked"]

        if result["execution_blocked"]:
            state["status"] = "safe_mode"

        self.state_manager.save(snapshot=False)
