#!/usr/bin/env bash
set -e

echo "=== SAFE AGENT RELEASE CHECK ==="

echo "[1/4] Git status"
git status --short
if [ -n "$(git status --short)" ]; then
    echo "ERROR: dirty tree"
    exit 1
fi

echo "[2/4] Unit tests"
./autonomous_agent/tools/test_all_v49.sh

echo "[3/4] End-to-end tests"
./autonomous_agent/tools/test_end_to_end_v49.sh

echo "[4/4] Version check"
python3 - <<'PY'
import safe_agent_v49 as agent
assert agent.VERSION == 49
print("VERSION OK:", agent.VERSION)
PY

echo
echo "=== RELEASE CHECK PASSED ==="
