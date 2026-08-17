#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " V44 END-TO-END"
echo "=========================================="

./tools/test_all_v44.sh

echo
echo "=== OLLAMA ==="

python3 - <<'PY'
import json
import os
import urllib.request

payload = {
    "model": os.environ["OLLAMA_MODEL"],
    "messages": [
        {
            "role": "user",
            "content": (
                'Antworte exakt mit '
                '{"action":"done","result":"OLLAMA_V44_OK"}'
            ),
        }
    ],
    "stream": False,
}

req = urllib.request.Request(
    os.environ["OLLAMA_URL"],
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json",
    },
)

with urllib.request.urlopen(
    req,
    timeout=60,
) as response:
    body = json.load(response)

content = body["message"]["content"]

print(content)

if "OLLAMA_V44_OK" not in content:
    raise SystemExit(
        "OLLAMA FAIL"
    )
PY

echo "OLLAMA V44 OK"

echo
echo "=== REAL AGENT ==="

rm -f runtime/v44_agent_test.txt

python3 safe_agent_v44.py \
    "Erstelle v44_agent_test.txt mit exakt V44_AGENT_OK. Lies die Datei danach wieder ein. Prüfe den Inhalt und antworte erst dann mit done."

test -f runtime/v44_agent_test.txt

test "$(
    cat runtime/v44_agent_test.txt
)" = "V44_AGENT_OK"

echo "REAL AGENT OK"

echo
echo "=== POLICY MODE ==="

SAFE_AGENT_POLICY=noexec \
python3 - <<'PY'
import safe_agent_v44 as agent

policy = agent.build_mission_policy()

assert policy["run_python"] is False
assert policy["write_file"] is True

print("NOEXEC POLICY OK")
PY

echo
echo "=== FINAL ==="

test -f safe_agent_v44.py

echo "V44 END-TO-END TESTS PASSED"
