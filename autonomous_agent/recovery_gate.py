class RecoveryGate:

    def __init__(self, governance):

        self.governance = governance


    def authorize_recovery(self, decision):

        result = self.governance.authorize(decision)

        authorization = result.get(
            "authorization",
            "BLOCK"
        )


        if authorization == "AUTO":

            return True


        if authorization == "VERIFY":

            return self.verify()


        return False



    def verify(self):

        return self.governance.state_manager.verify_snapshot_chain()
