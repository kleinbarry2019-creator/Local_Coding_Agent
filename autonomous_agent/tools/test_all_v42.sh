#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v42.py

echo
echo "=== SECURITY + TOOL TESTS ==="
python3 tests/test_v42.py

echo
echo "=== STATE ==="
python3 - <<'PY'
import safe_agent_v42 as agent

state = agent.load_state()
agent.verify_state_hash(state)

print(state)
print("STATE OK")
PY

echo
echo "=== ALL V42 TESTS PASSED ==="
