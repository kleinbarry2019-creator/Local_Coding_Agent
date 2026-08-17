#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=40

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

export OLLAMA_MODEL
export OLLAMA_URL
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT"

echo "=========================================="
echo " SAFE AGENT V40 - STANDALONE BUILD"
echo "=========================================="
echo "Workspace: $ROOT"
echo "Model:     $OLLAMA_MODEL"
echo

mkdir -p tests logs backups tools

cat > safe_agent_v40.py <<'PY'
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


VERSION = 40
ROOT = Path(__file__).resolve().parent

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


def default_state():
    return {
        "version": VERSION,
        "tasks_completed": 0,
        "last_result": None,
    }


def validate_state(state):
    if not isinstance(state, dict):
        raise RuntimeError("FAIL-CLOSED: State ist kein Objekt.")

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


def read_state_hash():
    if not STATE_HASH.exists():
        return None

    value = STATE_HASH.read_text(
        encoding="utf-8"
    ).strip()

    if len(value) != 64:
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash ungültig."
        )

    return value


def verify_state_hash(state, hash_path=None):
    if hash_path is None:
        hash_path = STATE_HASH

    if not hash_path.exists():
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash fehlt."
        )

    actual = hash_path.read_text(
        encoding="utf-8"
    ).strip()

    expected = state_digest(state)

    if actual != expected:
        raise RuntimeError(
            "FAIL-CLOSED: State-Hash stimmt nicht."
        )


def atomic_write(path: Path, data: str):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )

    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(
            tmp_path,
            path,
        )

    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def save_state(state):
    validate_state(state)

    encoded = json.dumps(
        state,
        indent=2,
        ensure_ascii=False,
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


def load_one_state(path: Path):
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
        verify_state_hash(
            current,
            STATE_HASH,
        )
        return current

    backup = load_one_state(
        STATE_BACKUP,
    )

    if backup is not None:
        save_state(backup)
        audit(
            "STATE_RECOVERY",
            {
                "source": str(STATE_BACKUP),
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


def audit(event, details):
    entry = {
        "timestamp": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "event": event,
        "details": details,
    }

    if AUDIT_FILE.exists():
        if (
            AUDIT_FILE.stat().st_size
            > MAX_AUDIT_BYTES
        ):
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
        data=json.dumps(payload).encode("utf-8"),
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


def sandbox_command(command):
    if not isinstance(command, str):
        raise PermissionError(
            "command muss ein String sein."
        )

    if not command.strip():
        raise PermissionError(
            "Leerer Befehl."
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

        "--proc", "/proc",
        "--dev", "/dev",

        "--tmpfs", "/etc",
        "--tmpfs", "/tmp",
        "--tmpfs", "/var",

        "--bind", str(ROOT), "/workspace",
        "--chdir", "/workspace",

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

    if set(obj) == {"action", "command"}:
        if obj["action"] != "run_command":
            raise RuntimeError(
                "FAIL-CLOSED: Ungültige Aktion."
            )

        if not isinstance(obj["command"], str):
            raise RuntimeError(
                "FAIL-CLOSED: command muss String sein."
            )

        return obj

    if set(obj) == {"action", "result"}:
        if obj["action"] != "done":
            raise RuntimeError(
                "FAIL-CLOSED: Ungültige Abschlussaktion."
            )

        if not isinstance(obj["result"], str):
            raise RuntimeError(
                "FAIL-CLOSED: result muss String sein."
            )

        return obj

    raise RuntimeError(
        "FAIL-CLOSED: Unbekanntes Aktionsschema."
    )


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
Du bist ein autonomer Entwickler-Agent.

Du arbeitest in /workspace.
Du darfst ausschließlich das bereitgestellte Sandbox-Kommando verwenden.

Antworte immer mit exakt einem JSON-Objekt.

Ausführen:
{"action":"run_command","command":"..."}

Fertig:
{"action":"done","result":"..."}

Kein Markdown.
Keine Erklärung außerhalb des JSON.
Keine mehreren JSON-Objekte.
Prüfe jedes Ergebnis, bevor du fortfährst.
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

    for step in range(1, MAX_STEPS + 1):
        print()
        print(f"=== STEP {step}/{MAX_STEPS} ===")

        response = ask_model(messages)

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
        print(json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ))

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
            'Usage: python3 safe_agent_v40.py "AUFGABE"'
        )
        raise SystemExit(2)

    task = " ".join(sys.argv[1:])

    print("=== SAFE AGENT V40 ===")
    print("Workspace:", ROOT)
    print("Model:", MODEL)

    run_agent(task)


if __name__ == "__main__":
    main()
PY

cat > tests/test_v40.py <<'PY'
from pathlib import Path
import safe_agent_v40 as agent


ROOT = agent.ROOT


def test_schema():
    agent.validate_state({
        "version": 40,
        "tasks_completed": 0,
        "last_result": None,
    })

    bad_states = [
        {
            "version": 40,
            "tasks_completed": -1,
            "last_result": None,
        },
        {
            "version": 40,
            "tasks_completed": 0,
        },
        {
            "version": 39,
            "tasks_completed": 0,
            "last_result": None,
        },
    ]

    for state in bad_states:
        try:
            agent.validate_state(state)
        except RuntimeError:
            continue
        raise AssertionError(
            f"Ungültiger State akzeptiert: {state!r}"
        )


def test_parser():
    result = agent.parse_action(
        '{"action":"done","result":"OK"}'
    )

    assert result["action"] == "done"

    try:
        agent.parse_action(
            '{"action":"run_command","command":"echo OK"}\n'
            '{"action":"done","result":"oops"}'
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "Mehrere JSON-Objekte wurden akzeptiert"
        )


def test_sandbox_visibility():
    result = agent.sandbox_command(
        "python3 -c '"
        "from pathlib import Path; "
        "print(Path(\"/var/home/mklein\").exists()); "
        "print(Path(\"/etc/shadow\").exists()); "
        "print(Path(\"/sys\").exists())'"
    )

    assert result["exit_code"] == 0

    lines = result["stdout"].splitlines()
    assert lines == ["False", "False", "False"]


def test_workspace_write():
    target = ROOT / "v40_test.txt"

    if target.exists():
        target.unlink()

    result = agent.sandbox_command(
        "printf 'V40_OK' > /workspace/v40_test.txt"
    )

    assert result["exit_code"] == 0
    assert target.read_text(
        encoding="utf-8"
    ) == "V40_OK"

    target.unlink()


def test_state_hash_roundtrip():
    state = {
        "version": 40,
        "tasks_completed": 3,
        "last_result": "OK",
    }

    digest = agent.state_digest(state)

    assert len(digest) == 64
    assert digest == agent.state_digest(state)


if __name__ == "__main__":
    tests = [
        test_schema,
        test_parser,
        test_sandbox_visibility,
        test_workspace_write,
        test_state_hash_roundtrip,
    ]

    for test in tests:
        print("[TEST]", test.__name__)
        test()

    print()
    print("V40 TESTS OK")
PY

cat > tools/test_all.sh <<'BASH2'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v40.py

echo "=== SECURITY TESTS ==="
python3 tests/test_v40.py

echo "=== STATE HASH ==="
python3 - <<'PY2'
import safe_agent_v40 as agent

state = agent.load_state()
print(state)
agent.verify_state_hash(state)
print("STATE HASH OK")
PY2

echo
echo "=== ALL V40 TESTS PASSED ==="
BASH2

chmod +x tools/test_all.sh

cat > tests/test_corrupt_state.sh <<'BASH3'
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
BASH3

chmod +x tests/test_corrupt_state.sh

cat > BUILD_INFO.txt <<EOF
SAFE AGENT V40
Built: $(date -Is)
Model: ${OLLAMA_MODEL}
Ollama URL: ${OLLAMA_URL}
Sandbox: bubblewrap
Network inside sandbox: disabled
Workspace: writable inside sandbox
Host filesystem: isolated
EOF

python3 -m py_compile safe_agent_v40.py

echo
echo "=== BUILD TEST ==="
./tools/test_all.sh

echo
echo "=== CORRUPTION / RECOVERY TEST ==="
./tests/test_corrupt_state.sh

echo
echo "=========================================="
echo " SAFE AGENT V40 READY"
echo "=========================================="
echo
echo 'Start:'
echo 'python3 safe_agent_v40.py "DEINE AUFGABE"'
echo
echo 'Tests:'
echo './tools/test_all.sh'
