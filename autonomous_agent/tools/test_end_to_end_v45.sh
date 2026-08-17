#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " V45 END-TO-END"
echo "=========================================="

./tools/test_all_v45.sh

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
                '{"action":"done","result":"OLLAMA_V45_OK"}'
            ),
        }
    ],
    "stream": False,
    "options": {
        "temperature": 0,
    },
}

request = urllib.request.Request(
    os.environ["OLLAMA_URL"],
    data=json.dumps(
        payload
    ).encode(),
    headers={
        "Content-Type": "application/json",
    },
)

with urllib.request.urlopen(
    request,
    timeout=60,
) as response:
    body = json.load(response)

content = body["message"]["content"]

print(content)

if "OLLAMA_V45_OK" not in content:
    raise SystemExit(
        "OLLAMA FAIL"
    )
PY

echo "OLLAMA V45 OK"

echo
echo "=== REAL AGENT ==="

rm -f runtime/v45_agent_test.txt

python3 safe_agent_v45.py \
    "Erstelle v45_agent_test.txt mit exakt V45_AGENT_OK. Lies die Datei danach wieder ein. Prüfe den Inhalt und antworte erst dann mit done."

test -f runtime/v45_agent_test.txt

test "$(
    cat runtime/v45_agent_test.txt
)" = "V45_AGENT_OK"

echo "REAL AGENT OK"

echo
echo "=== READONLY POLICY ==="

SAFE_AGENT_POLICY=readonly \
python3 - <<'PY'
import safe_agent_v45 as agent

policy = agent.build_mission_policy()

assert policy["read_file"]
assert not policy["write_file"]
assert not policy["run_python"]

print("READONLY POLICY OK")
PY

echo
echo "=== NOEXEC POLICY ==="

SAFE_AGENT_POLICY=noexec \
python3 - <<'PY'
import safe_agent_v45 as agent

policy = agent.build_mission_policy()

assert policy["write_file"]
assert not policy["run_python"]

print("NOEXEC POLICY OK")
PY

echo
echo "=== FINAL ==="

test -f safe_agent_v45.py

echo "AGENT CODE PRESENT"

echo
echo "V45 END-TO-END TESTS PASSED"
