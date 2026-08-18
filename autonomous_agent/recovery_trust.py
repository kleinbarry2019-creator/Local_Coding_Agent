from datetime import datetime


class RecoveryTrustEngine:

    MIN_TRUST = 0.75


    def __init__(self, state_manager):

        self.state_manager = state_manager


    def score(self):

        state = self.state_manager.state

        trust = state.get(
            "recovery_trust",
            1.0
        )

        failures = state.get(
            "recovery_failures",
            0
        )

        successes = state.get(
            "recovery_successes",
            0
        )


        trust -= failures * 0.20

        trust += successes * 0.05

        return max(
            0.0,
            min(
                trust,
                1.0
            )
        )


    def update(
        self,
        success
    ):

        state = self.state_manager.state


        if success:

            state["recovery_successes"] = (
                state.get(
                    "recovery_successes",
                    0
                ) + 1
            )

        else:

            state["recovery_failures"] = (
                state.get(
                    "recovery_failures",
                    0
                ) + 1
            )


        state["recovery_trust"] = self.score()

        state["last_trust_update"] = (
            datetime.utcnow().isoformat()
        )


        self.state_manager.save(
            snapshot=False
        )


        return state["recovery_trust"]


    def can_resume(self):

        return (
            self.score()
            >= self.MIN_TRUST
        )
