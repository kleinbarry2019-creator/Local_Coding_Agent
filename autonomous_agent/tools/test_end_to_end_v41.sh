#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " V41 END-TO-END TEST"
echo "=========================================="

echo
echo "=== BWRAP ==="
command -v bwrap
bwrap --version

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
                '{"action":"done","result":"OLLAMA_V41_OK"}'
            ),
        }
    ],
    "stream": False,
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

if "OLLAMA_V41_OK" not in content:
    raise SystemExit(
        "Ollama-Test fehlgeschlagen."
    )

print("OLLAMA V41 OK")
PY


echo
echo "=== SECURITY ==="
./tools/test_all_v41.sh


echo
echo "=== REAL AGENT TASK ==="

rm -f runtime/v41_agent_test.txt

python3 safe_agent_v41.py \
    "Erstelle in /workspace eine Datei v41_agent_test.txt mit exakt dem Inhalt V41_AGENT_OK. Prüfe danach den Inhalt. Wenn er exakt stimmt, antworte mit done."

test -f runtime/v41_agent_test.txt

test "$(
    cat runtime/v41_agent_test.txt
)" = "V41_AGENT_OK"

echo "REAL AGENT TASK OK"


echo
echo "=== AGENT SELF-PROTECTION ==="

test -f safe_agent_v41.py

echo "AGENT CODE STILL EXISTS"

echo
echo "=========================================="
echo " V41 END-TO-END TESTS PASSED"
echo "=========================================="
