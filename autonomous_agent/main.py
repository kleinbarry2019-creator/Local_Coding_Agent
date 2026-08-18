from autonomous_agent.orchestrator import Orchestrator
from autonomous_agent.memory_manager import MemoryManager
from autonomous_agent.self_monitor import SelfMonitor
from autonomous_agent.self_healer import SelfHealer
from autonomous_agent.validator import Validator
from autonomous_agent.state_manager import StateManager
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
    state.save(result)


if __name__ == "__main__":
    main()
