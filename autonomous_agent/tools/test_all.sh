#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v40.py

echo "=== SECURITY TESTS ==="
python3 tests/test_v40.py

echo "=== STATE HASH ==="
python3 - <<'PY2'
import safe_agent_v40 as agent

state = agent.load_state()
print(state)
agent.verify_state_hash(state)
print("STATE HASH OK")
PY2

echo
echo "=== ALL V40 TESTS PASSED ==="
