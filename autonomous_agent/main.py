from autonomous_agent.orchestrator import Orchestrator
from autonomous_agent.memory_manager import MemoryManager
from autonomous_agent.self_monitor import SelfMonitor
from autonomous_agent.self_healer import SelfHealer
from autonomous_agent.validator import Validator
from autonomous_agent.state_manager import StateManager
from autonomous_agent.recovery_policy import RecoveryPolicyEngine
from autonomous_agent.recovery_governance import RecoveryGovernanceEngine
from autonomous_agent.recovery_gate import RecoveryGate
from autonomous_agent.recovery_authority import RecoveryAuthority
from autonomous_agent.recovery_audit import RecoveryAuditChain
from autonomous_agent.recovery_trust import RecoveryTrustEngine
from autonomous_agent.recovery_controller import RecoveryController
from autonomous_agent.planner import Planner
from autonomous_agent.execution_engine import ExecutionEngine


def main():

    print("=== Autonomous Agent V50.6.2 ===")


    # Validation
    validator = Validator()

    validation = validator.check()

    print("Validation Check:")
    print(validation)


    # Monitoring
    monitor = SelfMonitor()

    status = monitor.system_report()

    print("System Check:")
    print(status)


    # Healing
    healer = SelfHealer()

    repair = healer.repair()

    print("Healing Check:")
    print(repair)


    # Memory
    memory = MemoryManager()


    # Persistent State
    state = StateManager()

    schema_recovery = state.validate_or_recover()

    print("State Schema Check:")
    print(schema_recovery)

    if not schema_recovery["accepted"]:
        print("State-Schema ungültig und kein sicherer Snapshot verfügbar")
        return

    policy = RecoveryPolicyEngine(state)

    recovery_decision = policy.execute()

    governance = RecoveryGovernanceEngine(state)

    authorization = governance.authorize(
        recovery_decision
    )

    state.state["recovery_confidence"] = authorization.get(
        "confidence",
        0
    )

    recovery_decision["confidence"] = authorization.get(
        "confidence",
        0
    )

    gate = RecoveryGate(
        governance
    )

    recovery_allowed = gate.authorize_recovery(
        recovery_decision
    )

    recovery_decision["confidence"] = authorization.get(
        "confidence",
        0.0
    )

    authority = RecoveryAuthority(
        state,
        recovery_decision,
        recovery_allowed
    )

    authority_decision = authority.evaluate()

    audit = RecoveryAuditChain(
        state
    )

    audit.append_event(
        {
            "authority": {
                "authority": authority_decision.get(
                    "authority"
                ),
                "execution_blocked": authority_decision.get(
                    "execution_blocked"
                ),
                "reason": authority_decision.get(
                    "reason"
                )
            },
            "policy": {
                "action": recovery_decision.get(
                    "action"
                ),
                "reason": recovery_decision.get(
                    "reason"
                ),
                "confidence": recovery_decision.get(
                    "confidence"
                )
            }
        }
    )

    print(
        "Recovery Authority:",
        authority_decision
    )

    print(
        "Recovery Audit:",
        audit.verify()
    )


    trust = RecoveryTrustEngine(
        state
    )


    controller = RecoveryController(
        state,
        trust,
        audit
    )


    resume = controller.attempt_resume()


    print(
        "Recovery Resume:",
        resume
    )

    if authority_decision["execution_blocked"]:

        if authority_decision["authority"] == "VERIFY":

            print(
                "Recovery VERIFY - Auditprüfung erforderlich"
            )

        else:

            print(
                "Recovery durch Authority blockiert"
            )

            return

    print(
        "Recovery Policy:",
        recovery_decision
    )

    print(
        "Recovery Governance:",
        authorization
    )

    print(
        "Recovery Gate:",
        recovery_allowed
    )

    if not recovery_allowed:
        state.state["status"] = "safe_mode"
        state.save(snapshot=False)

    # Automatic Snapshot Integrity Recovery
    try:
        if not state.verify_snapshot_chain():
            print("Snapshot Chain beschädigt")

            if recovery_allowed:

                print("Recovery Gate erlaubt Wiederherstellung")

                if state.restore_safe_point():
                    print("Safe Recovery erfolgreich")
                else:
                    print("Safe Recovery nicht möglich")

            else:

                print("Recovery Gate BLOCKIERT Wiederherstellung")

                state.state["status"] = "safe_mode"
                state.state["recovery_blocked"] = True
                state.save(snapshot=False)

        else:
            print("Snapshot Integrity Check erfolgreich")

        if state.recovered:
            state.state["recovery_event"] = True
            state.state.setdefault(
                "recovery_reason",
                "snapshot_integrity_failure"
            )
            state.save(snapshot=False)

    except Exception as e:
        print("Recovery Check Fehler:", e)


    previous_state = state.load()

    print("Previous State:")
    print(previous_state)


    # Core components
    planner = Planner()

    engine = ExecutionEngine()


    # Orchestrator
    orchestrator = Orchestrator(
        planner=planner,
        engine=engine,
        memory=memory,
        state=state
    )


    # Task
    task = {
        "goal": "create test file",
        "target": "test_agent_output.txt"
    }


    # Execute
    result = orchestrator.execute(task)


    print("Result:")
    print(result)


    # Save state
    state.save(result, snapshot=False)


if __name__ == "__main__":
    main()
