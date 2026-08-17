#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " V42 END-TO-END TEST"
echo "=========================================="

echo
echo "=== SECURITY TESTS ==="
./tools/test_all_v42.sh

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
                '{"action":"done","result":"OLLAMA_V42_OK"}'
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
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json"
    },
)

with urllib.request.urlopen(
    request,
    timeout=60,
) as response:
    result = json.load(response)

content = result["message"]["content"]

print(content)

if "OLLAMA_V42_OK" not in content:
    raise SystemExit(
        "OLLAMA TEST FAILED"
    )
PY

echo "OLLAMA V42 OK"

echo
echo "=== REAL AGENT TEST ==="

rm -f runtime/v42_agent_test.txt

python3 safe_agent_v42.py \
    "Erstelle v42_agent_test.txt mit exakt dem Inhalt V42_AGENT_OK. Lies die Datei danach wieder ein und prüfe den Inhalt. Antworte erst danach mit done."

test -f runtime/v42_agent_test.txt

test "$(
    cat runtime/v42_agent_test.txt
)" = "V42_AGENT_OK"

echo "REAL AGENT WRITE/READ OK"

echo
echo "=== SELF PROTECTION ==="

test -f safe_agent_v42.py

echo "AGENT CODE PRESENT"

echo
echo "=========================================="
echo " V42 END-TO-END TESTS PASSED"
echo "=========================================="
