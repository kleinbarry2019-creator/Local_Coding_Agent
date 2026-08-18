from autonomous_agent.orchestrator import Orchestrator
from autonomous_agent.memory_manager import MemoryManager
from autonomous_agent.self_monitor import SelfMonitor
from autonomous_agent.self_healer import SelfHealer
from autonomous_agent.planner import Planner
from autonomous_agent.execution_engine import ExecutionEngine


def main():

    print("=== Autonomous Agent V50.6.2 ===")

    monitor = SelfMonitor()
    print("System Check:")
    print(monitor.system_report())

    healer = SelfHealer()
    print("Healing Check:")
    print(healer.repair())

    memory = MemoryManager()
    planner = Planner()
    engine = ExecutionEngine()

    orchestrator = Orchestrator(
        planner=planner,
        engine=engine,
        memory=memory
    )

    task = {
        "goal": "create test file",
        "target": "test_agent_output.txt"
    }

    result = orchestrator.execute(task)

    print("Result:")
    print(result)


if __name__ == "__main__":
    main()
