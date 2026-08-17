#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " V43 END-TO-END TEST"
echo "=========================================="

echo
echo "=== SECURITY ==="
./tools/test_all_v43.sh

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
                '{"action":"done","result":"OLLAMA_V43_OK"}'
            ),
        }
    ],
    "stream": False,
    "options": {
        "temperature": 0,
    },
}

req = urllib.request.Request(
    os.environ["OLLAMA_URL"],
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json"
    },
)

with urllib.request.urlopen(
    req,
    timeout=60,
) as response:
    body = json.load(response)

content = body["message"]["content"]

print(content)

if "OLLAMA_V43_OK" not in content:
    raise SystemExit(
        "OLLAMA TEST FAILED"
    )
PY

echo "OLLAMA V43 OK"

echo
echo "=== REAL AGENT ==="

rm -f runtime/v43_agent_test.txt

python3 safe_agent_v43.py \
    "Erstelle v43_agent_test.txt mit exakt V43_AGENT_OK. Lies die Datei danach. Prüfe den Inhalt und antworte erst dann mit done."

test -f runtime/v43_agent_test.txt

test "$(
    cat runtime/v43_agent_test.txt
)" = "V43_AGENT_OK"

echo "REAL AGENT WRITE/READ OK"

echo
echo "=== AUDIT ==="

python3 - <<'PY'
import safe_agent_v43 as agent

agent.verify_audit_chain()

print("AUDIT CHAIN OK")
PY

echo
echo "=== SELF PROTECTION ==="

test -f safe_agent_v43.py

echo "AGENT CODE PRESENT"

echo
echo "=========================================="
echo " V43 END-TO-END TESTS PASSED"
echo "=========================================="
