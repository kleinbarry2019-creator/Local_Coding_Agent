#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v43.py

echo
echo "=== SECURITY TESTS ==="
python3 tests/test_v43.py

echo
echo "=== STATE ==="

python3 - <<'PY'
import safe_agent_v43 as agent

state = agent.load_state()

agent.verify_state_hash(
    state
)

agent.verify_audit_chain()

print(state)
print("STATE OK")
print("AUDIT CHAIN OK")
PY

echo
echo "=== ALL V43 TESTS PASSED ==="
