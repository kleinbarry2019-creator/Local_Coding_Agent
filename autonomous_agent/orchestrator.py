class AgentOrchestrator:

    def __init__(
        self,
        planner,
        loop,
        engine,
        state,
    ):
        self.planner = planner
        self.loop = loop
        self.engine = engine
        self.state = state


    def run(self, goal):

        plan = self.planner.create_plan(goal)

        self.state["goal"] = goal
        self.state["plan"] = plan
        self.state["status"] = "running"

        for step in plan:
            result = self.engine.execute(step)

            self.state["last_result"] = result

        self.state["status"] = "completed"

        return self.state
