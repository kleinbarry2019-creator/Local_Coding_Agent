class AgentOrchestrator:

    def __init__(
        self,
        planner=None,
        loop=None,
        engine=None,
        state=None,
        memory=None,
    ):
        self.planner = planner
        self.loop = loop
        self.engine = engine
        self.state = state or {}
        self.memory = memory


    def run(self, goal):

        self.state["goal"] = goal
        self.state["status"] = "running"

        if self.planner:
            plan = self.planner.create_plan(goal)
            self.state["plan"] = plan

            if self.engine:
                for step in plan:
                    result = self.engine.execute(step)
                    self.state["last_result"] = result

        if self.memory:
            self.memory.store(
                "last_goal",
                goal
            )

        self.state["status"] = "completed"

        return self.state


    def execute(self, goal):
        return self.run(goal)


Orchestrator = AgentOrchestrator
