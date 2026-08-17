#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

python3 -m py_compile safe_agent_v49.py

python3 tests/test_v49.py

python3 - <<'PY'
import safe_agent_v46 as agent

state = agent.load_state()

agent.verify_state_hash(
    state
)

agent.verify_audit_chain()

print("STATE OK")
print("AUDIT CHAIN OK")
PY

echo
echo "V49 TESTS PASSED"
