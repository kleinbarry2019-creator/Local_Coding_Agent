#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

python3 -m py_compile safe_agent_v50.py

echo "Using repaired V50 tests"

python3 - <<'PY'
import safe_agent_v50 as agent

state = agent.load_state()

agent.verify_state_hash(state)
print("STATE_VERIFY_OK")
PY

PYTHONPATH="$ROOT" python3 tests/test_v50.py

echo
echo "V50 TESTS PASSED"
