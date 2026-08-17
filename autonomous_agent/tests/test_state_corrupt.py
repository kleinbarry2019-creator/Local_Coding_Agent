from pathlib import Path

Path("agent_state.json").write_text(
    "BROKEN"
)

print(
    "CORRUPTION TEST CREATED"
)
