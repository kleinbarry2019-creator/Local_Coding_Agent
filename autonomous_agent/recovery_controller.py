from datetime import datetime


class RecoveryController:


    def __init__(
        self,
        state_manager,
        trust_engine,
        audit_chain
    ):

        self.state_manager = state_manager
        self.trust_engine = trust_engine
        self.audit_chain = audit_chain



    def validate_recovery(self):

        state = self.state_manager.state

        chain_ok = self.audit_chain.verify()

        trust = self.trust_engine.score()


        valid = (
            chain_ok
            and trust >= self.trust_engine.MIN_TRUST
        )


        self.audit_chain.append_event(
            {
                "event": "recovery_validation",
                "chain_valid": chain_ok,
                "trust": trust,
                "valid": valid
            }
        )


        return valid



    def attempt_resume(self):

        if self.validate_recovery():

            state = self.state_manager.state

            state["execution_blocked"] = False
            state["status"] = "recovered"
            state["recovery_event"] = False


            self.trust_engine.update(
                True
            )


            self.state_manager.save(
                snapshot=False
            )


            self.audit_chain.append_event(
                {
                    "event": "execution_resumed",
                    "timestamp": datetime.utcnow().isoformat()
                }
            )


            return {
                "resumed": True,
                "reason": "recovery_validated"
            }


        self.trust_engine.update(
            False
        )


        return {
            "resumed": False,
            "reason": "validation_failed"
        }
