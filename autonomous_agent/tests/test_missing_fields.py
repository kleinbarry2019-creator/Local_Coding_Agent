from pathlib import Path
import json

Path("agent_state.json").write_text(
    json.dumps(
        {
            "version":1,
            "tasks_completed":5
        }
    )
)

print(
    "SCHEMA TEST CREATED"
)
