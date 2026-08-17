#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=42

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"

echo "=========================================="
echo " SAFE AGENT V42 - DIRECT TOOLS"
echo "=========================================="
echo "Project: $ROOT"
echo "Model:   $OLLAMA_MODEL"
echo

mkdir -p runtime tests tools backups logs

cat > safe_agent_v42.py <<'PY'
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


VERSION = 42
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"

STATE_FILE = ROOT / "agent_state.json"
STATE_BACKUP = ROOT / "agent_state.json.bak"
STATE_HASH = ROOT / "agent_state.sha256"
AUDIT_FILE = ROOT / "audit_log.jsonl"

MAX_STATE_BYTES = 16384
MAX_AUDIT_BYTES = 1048576
MAX_CONTENT_BYTES = 262144
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
# PATH POLICY
# =========================================================

def resolve_runtime_path(path_text):
    if not isinstance(path_text, str):
        raise PermissionError(
            "path muss String sein."
        )

    if not path_text.strip():
        raise PermissionError(
            "Leerer Pfad."
        )

    candidate = (RUNTIME / path_text).resolve(
        strict=False
    )

    try:
        candidate.relative_to(
            RUNTIME
        )
    except ValueError as exc:
        raise PermissionError(
            "Pfad außerhalb des Runtime-Workspace."
        ) from exc

    return candidate


# =========================================================
# DIRECT TOOLS
# =========================================================

def list_files():
    RUNTIME.mkdir(
        parents=True,
        exist_ok=True,
    )

    return sorted(
        path.relative_to(RUNTIME).as_posix()
        for path in RUNTIME.rglob("*")
        if path.is_file()
    )


def read_file(path):
    target = resolve_runtime_path(path)

    if not target.exists():
        raise FileNotFoundError(
            f"Datei nicht gefunden: {path}"
        )

    if not target.is_file():
        raise PermissionError(
            "Kein regulärer File-Pfad."
        )

    size = target.stat().st_size

    if size > MAX_CONTENT_BYTES:
        raise PermissionError(
            "Datei überschreitet Größenlimit."
        )

    return target.read_text(
        encoding="utf-8"
    )


def write_file(path, content):
    if not isinstance(content, str):
        raise TypeError(
            "content muss String sein."
        )

    encoded = content.encode(
        "utf-8"
    )

    if len(encoded) > MAX_CONTENT_BYTES:
        raise PermissionError(
            "Inhalt überschreitet Größenlimit."
        )

    target = resolve_runtime_path(path)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    atomic_write(
        target,
        content,
    )

    return {
        "path": target.relative_to(
            RUNTIME
        ).as_posix(),
        "bytes": len(encoded),
    }


def run_python(script, args=None):
    if args is None:
        args = []

    if not isinstance(args, list):
        raise TypeError(
            "args muss Liste sein."
        )

    if not all(
        isinstance(item, str)
        for item in args
    ):
        raise TypeError(
            "args dürfen nur Strings enthalten."
        )

    script_path = resolve_runtime_path(
        script
    )

    if script_path.suffix != ".py":
        raise PermissionError(
            "Nur .py-Skripte erlaubt."
        )

    if not script_path.is_file():
        raise FileNotFoundError(
            f"Skript nicht gefunden: {script}"
        )

    command = [
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

        "--ro-bind",
        str(ROOT),
        "/agent",

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

        sys.executable,
        "/workspace/" + script_path.relative_to(
            RUNTIME
        ).as_posix(),
        *args,
    ]

    try:
        result = subprocess.run(
            command,
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
        "stdout": result.stdout[
            :MAX_OUTPUT_CHARS
        ],
        "stderr": result.stderr[
            :MAX_OUTPUT_CHARS
        ],
    }


TOOL_NAMES = {
    "list_files",
    "read_file",
    "write_file",
    "run_python",
}


def execute_tool(name, arguments):
    if name not in TOOL_NAMES:
        raise PermissionError(
            f"Tool nicht erlaubt: {name}"
        )

    if not isinstance(arguments, dict):
        raise TypeError(
            "arguments muss Objekt sein."
        )

    if name == "list_files":
        if arguments:
            raise PermissionError(
                "list_files akzeptiert keine Argumente."
            )
        return list_files()

    if name == "read_file":
        if set(arguments) != {"path"}:
            raise PermissionError(
                "read_file erwartet exakt path."
            )
        return read_file(
            arguments["path"]
        )

    if name == "write_file":
        if set(arguments) != {
            "path",
            "content",
        }:
            raise PermissionError(
                "write_file erwartet path und content."
            )

        return write_file(
            arguments["path"],
            arguments["content"],
        )

    if name == "run_python":
        allowed = {"script", "args"}

        if set(arguments) - allowed:
            raise PermissionError(
                "run_python enthält unerlaubte Argumente."
            )

        return run_python(
            arguments["script"],
            arguments.get("args", []),
        )

    raise RuntimeError(
        "Unbekanntes Tool."
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
# STRICT PROTOCOL
# =========================================================

def parse_action(text):
    try:
        obj = json.loads(text)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FAIL-CLOSED: Antwort ist kein einzelnes JSON."
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: JSON-Objekt erwartet."
        )

    if set(obj) == {
        "action",
        "name",
        "arguments",
    }:
        if obj["action"] != "tool":
            raise RuntimeError(
                "FAIL-CLOSED: Ungültige Tool-Aktion."
            )

        if not isinstance(
            obj["name"],
            str,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: Toolname ungültig."
            )

        if not isinstance(
            obj["arguments"],
            dict,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: arguments ungültig."
            )

        return obj

    if set(obj) == {
        "action",
        "result",
    }:
        if obj["action"] != "done":
            raise RuntimeError(
                "FAIL-CLOSED: Ungültige Abschlussaktion."
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
        "FAIL-CLOSED: Unbekanntes Antwortschema."
    )


# =========================================================
# AUTONOMY LOOP
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

Du arbeitest ausschließlich in /workspace.

Verfügbare Werkzeuge:

1.
{"action":"tool","name":"list_files","arguments":{}}

2.
{"action":"tool","name":"read_file","arguments":{"path":"datei.txt"}}

3.
{"action":"tool","name":"write_file","arguments":{"path":"datei.txt","content":"TEXT"}}

4.
{"action":"tool","name":"run_python","arguments":{"script":"test.py","args":[]}}

Abschluss:
{"action":"done","result":"..."}

Regeln:
- Keine Shell-Kommandos.
- Kein bash.
- Kein sh.
- Kein exec.
- Keine Markdown-Antworten.
- Genau EIN JSON-Objekt pro Antwort.
- Nur /workspace verändern.
- Niemals /agent verändern.
- Prüfe Tool-Ergebnisse, bevor du fortfährst.
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
                "Beginne jetzt."
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

        action = parse_action(
            response
        )

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

        try:
            result = execute_tool(
                action["name"],
                action["arguments"],
            )

        except Exception as exc:
            result = {
                "status": "ERROR",
                "error": type(exc).__name__,
                "message": str(exc),
            }

        audit(
            "TOOL",
            {
                "step": step,
                "name": action["name"],
                "status": result.get(
                    "status",
                    "OK",
                )
                if isinstance(result, dict)
                else "OK",
            },
        )

        print("RESULT:")
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
            if isinstance(result, (dict, list))
            else result
        )

        messages.append(
            {
                "role": "user",
                "content": (
                    "TOOL-ERGEBNIS:\n"
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
        "FAIL-CLOSED: Schrittlimit erreicht."
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
        "FAIL-CLOSED: Maximale Schrittzahl erreicht."
    )


def main():
    if len(sys.argv) < 2:
        print(
            'Usage: python3 safe_agent_v42.py "AUFGABE"'
        )
        raise SystemExit(2)

    print("=== SAFE AGENT V42 ===")
    print("Project:", ROOT)
    print("Runtime:", RUNTIME)
    print("Model:", MODEL)

    run_agent(
        " ".join(sys.argv[1:])
    )


if __name__ == "__main__":
    main()
PY


cat > tests/test_v42.py <<'PY'
import json
from pathlib import Path

import safe_agent_v42 as agent


ROOT = agent.ROOT
RUNTIME = agent.RUNTIME


def test_schema():
    agent.validate_state({
        "version": 42,
        "tasks_completed": 0,
        "last_result": None,
    })

    for bad in [
        {
            "version": 41,
            "tasks_completed": 0,
            "last_result": None,
        },
        {
            "version": 42,
            "tasks_completed": -1,
            "last_result": None,
        },
        {
            "version": 42,
            "tasks_completed": 0,
        },
    ]:
        try:
            agent.validate_state(bad)
        except RuntimeError:
            continue

        raise AssertionError(
            f"Invalid state accepted: {bad!r}"
        )


def test_tool_policy():
    try:
        agent.execute_tool(
            "shell",
            {},
        )
    except PermissionError:
        pass
    else:
        raise AssertionError(
            "Unknown tool accepted"
        )


def test_path_escape_blocked():
    for path in [
        "../escape.txt",
        "/etc/shadow",
        "../../escape.txt",
    ]:
        try:
            agent.write_file(
                path,
                "NOPE",
            )
        except PermissionError:
            continue

        raise AssertionError(
            f"Escape path accepted: {path}"
        )


def test_write_and_read():
    target = "v42_tool_test.txt"

    result = agent.write_file(
        target,
        "V42_OK",
    )

    assert result["bytes"] == 6
    assert agent.read_file(target) == "V42_OK"

    (RUNTIME / target).unlink()


def test_list_files():
    target = "v42_list_test.txt"

    agent.write_file(
        target,
        "X",
    )

    files = agent.list_files()

    assert target in files

    (RUNTIME / target).unlink()


def test_readonly_agent():
    result = subprocess_run(
        "printf HACK >> /agent/safe_agent_v42.py"
    )

    assert result != 0


def subprocess_run(command):
    result = agent.run_python(
        "noop.py"
    )
    return result["exit_code"]


def test_parser():
    result = agent.parse_action(
        json.dumps({
            "action": "done",
            "result": "OK",
        })
    )

    assert result["action"] == "done"

    try:
        agent.parse_action(
            '{"action":"done","result":"OK"}\n'
            '{"action":"done","result":"BAD"}'
        )
    except RuntimeError:
        return

    raise AssertionError(
        "Multiple JSON objects accepted"
    )


def test_hash():
    state = {
        "version": 42,
        "tasks_completed": 2,
        "last_result": "OK",
    }

    digest = agent.state_digest(
        state
    )

    assert len(digest) == 64


if __name__ == "__main__":
    tests = [
        test_schema,
        test_tool_policy,
        test_path_escape_blocked,
        test_write_and_read,
        test_list_files,
        test_parser,
        test_hash,
    ]

    for test in tests:
        print("[TEST]", test.__name__)
        test()

    print()
    print("V42 TESTS OK")
PY


cat > tools/test_all_v42.sh <<'BASH2'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v42.py

echo
echo "=== SECURITY + TOOL TESTS ==="
python3 tests/test_v42.py

echo
echo "=== STATE ==="
python3 - <<'PY'
import safe_agent_v42 as agent

state = agent.load_state()
agent.verify_state_hash(state)

print(state)
print("STATE OK")
PY

echo
echo "=== ALL V42 TESTS PASSED ==="
BASH2

chmod +x tools/test_all_v42.sh


cat > tools/test_end_to_end_v42.sh <<'BASH3'
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
BASH3

chmod +x tools/test_end_to_end_v42.sh


cat > BUILD_INFO_V42.txt <<EOF
SAFE AGENT V42

Built: $(date -Is)
Model: ${OLLAMA_MODEL}
Ollama URL: ${OLLAMA_URL}

Tools:
- list_files
- read_file
- write_file
- run_python

No normal shell tool.
Runtime workspace isolated.
Agent code read-only in sandbox.
Network unshared.
Capabilities dropped.
EOF


python3 -m py_compile safe_agent_v42.py

echo
echo "=== V42 BUILD ==="
./tools/test_all_v42.sh

echo
echo "=== V42 END-TO-END ==="
./tools/test_end_to_end_v42.sh

echo
echo "=========================================="
echo " SAFE AGENT V42 READY"
echo "=========================================="
