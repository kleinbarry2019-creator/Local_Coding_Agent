#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=41

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT"

echo "=========================================="
echo " SAFE AGENT V41 - ISOLATED RUNTIME"
echo "=========================================="
echo "Project: $ROOT"
echo "Model:   $OLLAMA_MODEL"
echo

mkdir -p \
    runtime \
    tests \
    tools \
    logs \
    backups


cat > safe_agent_v41.py <<'PY'
#!/usr/bin/env python3

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


VERSION = 41

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"

STATE_FILE = ROOT / "agent_state.json"
STATE_BACKUP = ROOT / "agent_state.json.bak"
STATE_HASH = ROOT / "agent_state.sha256"
AUDIT_FILE = ROOT / "audit_log.jsonl"

MAX_STATE_BYTES = 16384
MAX_AUDIT_BYTES = 1048576
MAX_OUTPUT_CHARS = 8192
MAX_STEPS = 20
COMMAND_TIMEOUT = 15

OLLAMA_URL = os.environ.get(
    "OLLAMA_URL",
    "http://127.0.0.1:11434/api/chat",
)

MODEL = os.environ.get(
    "OLLAMA_MODEL",
    "qwen2.5-coder:7b-instruct",
)


# =========================================================
# STATE
# =========================================================

def default_state():
    return {
        "version": VERSION,
        "tasks_completed": 0,
        "last_result": None,
    }


def validate_state(state):
    if not isinstance(state, dict):
        raise RuntimeError(
            "FAIL-CLOSED: State ist kein Objekt."
        )

    required = {
        "version",
        "tasks_completed",
        "last_result",
    }

    missing = required - set(state)

    if missing:
        raise RuntimeError(
            f"FAIL-CLOSED: State-Felder fehlen: {sorted(missing)}"
        )

    if state["version"] != VERSION:
        raise RuntimeError(
            f"FAIL-CLOSED: Falsche State-Version: {state['version']}"
        )

    if (
        not isinstance(state["tasks_completed"], int)
        or isinstance(state["tasks_completed"], bool)
        or state["tasks_completed"] < 0
    ):
        raise RuntimeError(
            "FAIL-CLOSED: tasks_completed ungültig."
        )

    if (
        state["last_result"] is not None
        and not isinstance(state["last_result"], str)
    ):
        raise RuntimeError(
            "FAIL-CLOSED: last_result ungültig."
        )

    return state


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def state_digest(state):
    return hashlib.sha256(
        canonical_json(state).encode("utf-8")
    ).hexdigest()


def atomic_write(path, data):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )

    tmp = Path(tmp_name)

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp, path)

    finally:
        if tmp.exists():
            tmp.unlink()


def verify_state_hash(state):
    if not STATE_HASH.exists():
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash fehlt."
        )

    actual = STATE_HASH.read_text(
        encoding="utf-8"
    ).strip()

    expected = state_digest(state)

    if actual != expected:
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash stimmt nicht."
        )


def save_state(state):
    validate_state(state)

    encoded = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    )

    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise RuntimeError(
            "FAIL-CLOSED: State zu groß."
        )

    if STATE_FILE.exists():
        shutil.copy2(
            STATE_FILE,
            STATE_BACKUP,
        )

    atomic_write(
        STATE_FILE,
        encoded + "\n",
    )

    atomic_write(
        STATE_HASH,
        state_digest(state) + "\n",
    )


def load_one_state(path):
    if not path.exists():
        return None

    if path.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeError(
            f"FAIL-CLOSED: State zu groß: {path}"
        )

    try:
        state = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
        return validate_state(state)

    except Exception:
        return None


def load_state():
    current = load_one_state(
        STATE_FILE
    )

    if current is not None:
        verify_state_hash(current)
        return current

    backup = load_one_state(
        STATE_BACKUP
    )

    if backup is not None:
        save_state(backup)

        audit(
            "STATE_RECOVERY",
            {
                "source": str(STATE_BACKUP)
            },
        )

        return backup

    state = default_state()
    save_state(state)

    audit(
        "STATE_INITIALIZED",
        {},
    )

    return state


# =========================================================
# AUDIT
# =========================================================

def audit(event, details):
    entry = {
        "timestamp": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "event": event,
        "details": details,
    }

    if AUDIT_FILE.exists():
        if AUDIT_FILE.stat().st_size > MAX_AUDIT_BYTES:
            raise RuntimeError(
                "FAIL-CLOSED: Audit-Log zu groß."
            )

    with AUDIT_FILE.open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


# =========================================================
# MODEL
# =========================================================

def ask_model(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0,
        },
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(
            payload
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:
            body = json.load(response)

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Ollama HTTP {exc.code}: {detail}"
        ) from exc

    except Exception as exc:
        raise RuntimeError(
            f"Ollama nicht erreichbar: {exc}"
        ) from exc

    message = body.get("message")

    if not isinstance(message, dict):
        raise RuntimeError(
            "Ollama-Antwort enthält keine message."
        )

    content = message.get("content")

    if not isinstance(content, str):
        raise RuntimeError(
            "Ollama-Antwort enthält keinen Text."
        )

    return content.strip()


# =========================================================
# SANDBOX
# =========================================================

def sandbox_command(command):
    if not isinstance(command, str):
        raise PermissionError(
            "command muss String sein."
        )

    if not command.strip():
        raise PermissionError(
            "Leerer Befehl."
        )

    RUNTIME.mkdir(
        parents=True,
        exist_ok=True,
    )

    argv = [
        "/usr/bin/bwrap",

        "--die-with-parent",

        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup",

        "--cap-drop", "ALL",

        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",

        # Agent-Projekt nur read-only
        "--ro-bind",
        str(ROOT),
        "/agent",

        # Aufgaben-Workspace separat beschreibbar
        "--bind",
        str(RUNTIME),
        "/workspace",

        "--proc", "/proc",
        "--dev", "/dev",

        "--tmpfs", "/etc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var",

        "--chdir",
        "/workspace",

        "/bin/bash",
        "-lc",
        command,
    ]

    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT,
            shell=False,
            check=False,
        )

    except subprocess.TimeoutExpired as exc:
        return {
            "status": "TIMEOUT",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
        }

    return {
        "status": (
            "OK"
            if result.returncode == 0
            else "ERROR"
        ),
        "exit_code": result.returncode,
        "stdout": result.stdout[:MAX_OUTPUT_CHARS],
        "stderr": result.stderr[:MAX_OUTPUT_CHARS],
    }


# =========================================================
# PROTOCOL
# =========================================================

def parse_action(text):
    try:
        obj = json.loads(text)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort ist kein einzelnes JSON."
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: JSON-Objekt erwartet."
        )

    if set(obj) == {
        "action",
        "command",
    }:
        if obj["action"] != "run_command":
            raise RuntimeError(
                "FAIL-CLOSED: ungültige Aktion."
            )

        if not isinstance(
            obj["command"],
            str,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: command ungültig."
            )

        return obj

    if set(obj) == {
        "action",
        "result",
    }:
        if obj["action"] != "done":
            raise RuntimeError(
                "FAIL-CLOSED: ungültige Abschlussaktion."
            )

        if not isinstance(
            obj["result"],
            str,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: result ungültig."
            )

        return obj

    raise RuntimeError(
        "FAIL-CLOSED: unbekanntes Antwortschema."
    )


# =========================================================
# AGENT LOOP
# =========================================================

def run_agent(task):
    state = load_state()

    audit(
        "MISSION_START",
        {
            "task": task,
            "model": MODEL,
        },
    )

    system_prompt = """
Du bist ein sicherer autonomer Entwickler-Agent.

Der einzige beschreibbare Arbeitsbereich ist /workspace.

WICHTIG:
- /agent ist nur lesbar.
- Verändere niemals /agent.
- Arbeite ausschließlich in /workspace.
- Verwende keine Netzwerkzugriffe.
- Verwende ausschließlich JSON.

Ausführen:
{"action":"run_command","command":"..."}

Fertig:
{"action":"done","result":"..."}

Kein Markdown.
Keine zusätzlichen JSON-Objekte.
Keine Erklärungen außerhalb des JSON.
"""

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": (
                "AUFGABE:\n"
                + task
                + "\n"
                "Beginne mit dem ersten sinnvollen Schritt."
            ),
        },
    ]

    for step in range(
        1,
        MAX_STEPS + 1,
    ):
        print()
        print(
            f"=== STEP {step}/{MAX_STEPS} ==="
        )

        response = ask_model(
            messages
        )

        print("MODEL:")
        print(response)

        action = parse_action(response)

        messages.append(
            {
                "role": "assistant",
                "content": response,
            }
        )

        if action["action"] == "done":
            result = action["result"]

            state["tasks_completed"] += 1
            state["last_result"] = result

            save_state(state)

            audit(
                "MISSION_DONE",
                {
                    "step": step,
                    "result": result,
                },
            )

            print()
            print("=== DONE ===")
            print(result)
            return

        result = sandbox_command(
            action["command"]
        )

        audit(
            "COMMAND",
            {
                "step": step,
                "command": action["command"],
                "status": result["status"],
                "exit_code": result["exit_code"],
            },
        )

        print("RESULT:")
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

        messages.append(
            {
                "role": "user",
                "content": (
                    "ERGEBNIS:\n"
                    + json.dumps(
                        result,
                        ensure_ascii=False,
                    )
                    + "\n"
                    "Analysiere das Ergebnis und fahre fort."
                ),
            }
        )

    state["last_result"] = (
        "FAIL-CLOSED: Maximale Schrittzahl erreicht."
    )

    save_state(state)

    audit(
        "MISSION_ABORTED",
        {
            "reason": "step_limit",
            "max_steps": MAX_STEPS,
        },
    )

    raise RuntimeError(
        "FAIL-CLOSED: Schrittlimit erreicht."
    )


def main():
    if len(sys.argv) < 2:
        print(
            'Usage: python3 safe_agent_v41.py "AUFGABE"'
        )
        raise SystemExit(2)

    task = " ".join(
        sys.argv[1:]
    )

    print("=== SAFE AGENT V41 ===")
    print("Project:", ROOT)
    print("Runtime:", RUNTIME)
    print("Model:", MODEL)

    run_agent(task)


if __name__ == "__main__":
    main()
PY


cat > tests/test_v41.py <<'PY'
from pathlib import Path
import safe_agent_v41 as agent


ROOT = agent.ROOT
RUNTIME = agent.RUNTIME


def test_schema():
    agent.validate_state({
        "version": 41,
        "tasks_completed": 0,
        "last_result": None,
    })

    bad = [
        {
            "version": 41,
            "tasks_completed": -1,
            "last_result": None,
        },
        {
            "version": 40,
            "tasks_completed": 0,
            "last_result": None,
        },
        {
            "version": 41,
            "tasks_completed": 0,
        },
    ]

    for state in bad:
        try:
            agent.validate_state(state)
        except RuntimeError:
            continue

        raise AssertionError(
            f"State wurde akzeptiert: {state!r}"
        )


def test_workspace_write():
    target = RUNTIME / "v41_test.txt"

    if target.exists():
        target.unlink()

    result = agent.sandbox_command(
        "printf 'V41_OK' > /workspace/v41_test.txt"
    )

    assert result["exit_code"] == 0
    assert target.read_text(
        encoding="utf-8"
    ) == "V41_OK"

    target.unlink()


def test_agent_code_readonly():
    result = agent.sandbox_command(
        "printf 'HACK' >> /agent/safe_agent_v41.py"
    )

    assert result["exit_code"] != 0


def test_agent_code_not_replaceable():
    result = agent.sandbox_command(
        "rm -f /agent/safe_agent_v41.py"
    )

    assert result["exit_code"] != 0
    assert (
        ROOT / "safe_agent_v41.py"
    ).exists()


def test_host_hidden():
    result = agent.sandbox_command(
        "python3 -c '"
        "from pathlib import Path; "
        "print(Path(\"/var/home/mklein\").exists()); "
        "print(Path(\"/etc/shadow\").exists()); "
        "print(Path(\"/sys\").exists())'"
    )

    assert result["exit_code"] == 0

    assert result["stdout"].splitlines() == [
        "False",
        "False",
        "False",
    ]


def test_agent_readonly_visible():
    result = agent.sandbox_command(
        "test -r /agent/safe_agent_v41.py"
    )

    assert result["exit_code"] == 0


def test_hash():
    state = {
        "version": 41,
        "tasks_completed": 1,
        "last_result": "OK",
    }

    digest = agent.state_digest(state)

    assert len(digest) == 64


if __name__ == "__main__":
    tests = [
        test_schema,
        test_workspace_write,
        test_agent_code_readonly,
        test_agent_code_not_replaceable,
        test_host_hidden,
        test_agent_readonly_visible,
        test_hash,
    ]

    for test in tests:
        print("[TEST]", test.__name__)
        test()

    print()
    print("V41 TESTS OK")
PY


cat > tools/test_all_v41.sh <<'BASH2'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v41.py

echo
echo "=== V41 SECURITY TESTS ==="
python3 tests/test_v41.py

echo
echo "=== STATE ==="
python3 - <<'PY'
import safe_agent_v41 as agent

state = agent.load_state()
agent.verify_state_hash(state)

print(state)
print("STATE OK")
PY

echo
echo "=== ALL V41 TESTS PASSED ==="
BASH2

chmod +x tools/test_all_v41.sh


cat > tools/test_end_to_end_v41.sh <<'BASH3'
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
BASH3

chmod +x tools/test_end_to_end_v41.sh


cat > BUILD_INFO_V41.txt <<EOF
SAFE AGENT V41

Built: $(date -Is)

Model: ${OLLAMA_MODEL}
Ollama URL: ${OLLAMA_URL}

Agent code:
READ-ONLY inside sandbox as /agent

Task workspace:
WRITABLE inside sandbox as /workspace

Host home:
HIDDEN

Network:
UNSHARED

Capabilities:
DROPPED
EOF


python3 -m py_compile safe_agent_v41.py

echo
echo "=== V41 UNIT + SECURITY ==="

./tools/test_all_v41.sh

echo
echo "=== V41 END-TO-END ==="

./tools/test_end_to_end_v41.sh

echo
echo "=========================================="
echo " SAFE AGENT V41 READY"
echo "=========================================="
