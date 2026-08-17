#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

echo "=========================================="
echo " SAFE AGENT V40 END-TO-END TEST"
echo "=========================================="

echo
echo "=== 1. BUBBLEWRAP ==="
command -v bwrap
bwrap --version

echo
echo "=== 2. OLLAMA ==="

python3 - <<'PY'
import json
import os
import urllib.request

url = os.environ["OLLAMA_URL"]

payload = {
    "model": os.environ["OLLAMA_MODEL"],
    "messages": [
        {
            "role": "user",
            "content": (
                'Antworte exakt mit diesem JSON: '
                '{"action":"done","result":"OLLAMA_OK"}'
            ),
        }
    ],
    "stream": False,
    "options": {
        "temperature": 0
    }
}

request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode(),
    headers={
        "Content-Type": "application/json"
    }
)

with urllib.request.urlopen(
    request,
    timeout=60
) as response:
    result = json.load(response)

message = result.get("message", {})
content = message.get("content", "")

print("MODEL:", os.environ["OLLAMA_MODEL"])
print("RESPONSE:", content)

if "OLLAMA_OK" not in content:
    raise SystemExit(
        "FAIL: Ollama liefert nicht das erwartete Ergebnis."
    )

print("OLLAMA TEST OK")
PY

echo
echo "=== 3. V40 UNIT + SECURITY TESTS ==="
./tools/test_all.sh

echo
echo "=== 4. DIRECT SANDBOX TEST ==="

python3 - <<'PY'
import safe_agent_v40 as agent

result = agent.sandbox_command(
    "printf 'E2E_OK' > /workspace/e2e_test.txt"
)

print(result)

if result["exit_code"] != 0:
    raise SystemExit(
        "FAIL: Sandbox-Kommando fehlgeschlagen."
    )
PY

test "$(cat e2e_test.txt)" = "E2E_OK"
rm -f e2e_test.txt

echo "SANDBOX WRITE OK"

echo
echo "=== 5. HOST VISIBILITY TEST ==="

python3 - <<'PY'
import safe_agent_v40 as agent

result = agent.sandbox_command(
    "python3 -c '"
    "from pathlib import Path; "
    "print(\"HOME=\" + str(Path(\"/var/home/mklein\").exists())); "
    "print(\"SHADOW=\" + str(Path(\"/etc/shadow\").exists())); "
    "print(\"SYS=\" + str(Path(\"/sys\").exists()))'"
)

print(result)

if result["exit_code"] != 0:
    raise SystemExit(
        "FAIL: Sandbox visibility test fehlgeschlagen."
    )

if result["stdout"].splitlines() != [
    "HOME=False",
    "SHADOW=False",
    "SYS=False",
]:
    raise SystemExit(
        "FAIL: Host-Isolation entspricht nicht der Erwartung."
    )

print("HOST ISOLATION OK")
PY

echo
echo "=== 6. STATE ==="

python3 - <<'PY'
import safe_agent_v40 as agent

state = agent.load_state()
agent.verify_state_hash(state)

print(state)
print("STATE OK")
PY

echo
echo "=========================================="
echo " V40 END-TO-END TESTS PASSED"
echo "=========================================="
