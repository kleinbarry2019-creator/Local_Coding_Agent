#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

cp -f agent_state.json agent_state.json.test-backup
cp -f agent_state.sha256 agent_state.sha256.test-backup

restore() {
    mv -f agent_state.json.test-backup agent_state.json
    mv -f agent_state.sha256.test-backup agent_state.sha256
}

trap restore EXIT

printf '%s\n' 'BROKEN' > agent_state.json

python3 - <<'PY'
import safe_agent_v40 as agent

state = agent.load_state()

assert state["version"] == 40
print("PASS: Recovery aus Backup")
PY

echo "CORRUPTION TEST OK"
