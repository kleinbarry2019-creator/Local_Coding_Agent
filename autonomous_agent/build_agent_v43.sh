#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION=43

OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5-coder:7b-instruct}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434/api/chat}"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT"

# ---------------------------------------------------------
# V43 AUDIT MIGRATION
# ---------------------------------------------------------
# Ältere V40/V41/V42-Auditdateien verwenden ein anderes
# Format. Nicht konvertieren, sondern sicher archivieren.
# V43 startet eine neue, prüfbare Audit-Kette.

mkdir -p logs

if [[ -f audit_log.jsonl ]]; then
    if [[ -s audit_log.jsonl ]]; then
        backup="logs/audit_pre_v43_$(date +%Y%m%d_%H%M%S).jsonl"
        cp -f audit_log.jsonl "$backup"
        echo "Altes Audit archiviert: $backup"
    fi
    rm -f audit_log.jsonl
fi

rm -f audit_head.sha256


echo "=========================================="
echo " SAFE AGENT V43 - HARDENED TOOLS"
echo "=========================================="
echo "Project: $ROOT"
echo "Model:   $OLLAMA_MODEL"
echo

mkdir -p runtime tests tools backups logs

cat > safe_agent_v43.py <<'PY'
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


VERSION = 43
ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"

STATE_FILE = ROOT / "agent_state.json"
STATE_BACKUP = ROOT / "agent_state.json.bak"
STATE_HASH = ROOT / "agent_state.sha256"

AUDIT_FILE = ROOT / "audit_log.jsonl"
AUDIT_HEAD_FILE = ROOT / "audit_head.sha256"

MAX_STATE_BYTES = 16384
MAX_AUDIT_BYTES = 1048576
MAX_CONTENT_BYTES = 262144
MAX_OUTPUT_CHARS = 8192
MAX_STEPS = 20
MAX_SCRIPT_ARGS = 16
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
# AUDIT CHAIN
# =========================================================

def audit_digest(entry, previous):
    material = (
        previous
        + "\n"
        + canonical_json(entry)
    )

    return hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()


def read_audit_head():
    if not AUDIT_HEAD_FILE.exists():
        return "0" * 64

    value = AUDIT_HEAD_FILE.read_text(
        encoding="utf-8"
    ).strip()

    if len(value) != 64:
        raise RuntimeError(
            "FAIL-CLOSED: Audit-Head ungültig."
        )

    return value


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

    previous = read_audit_head()

    digest = audit_digest(
        entry,
        previous,
    )

    record = {
        "prev": previous,
        "digest": digest,
        "entry": entry,
    }

    with AUDIT_FILE.open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )

    atomic_write(
        AUDIT_HEAD_FILE,
        digest + "\n",
    )


def verify_audit_chain():
    if not AUDIT_FILE.exists():
        return True

    previous = "0" * 64

    with AUDIT_FILE.open(
        "r",
        encoding="utf-8",
    ) as handle:
        for line_no, line in enumerate(
            handle,
            1,
        ):
            if not line.strip():
                continue

            record = json.loads(line)

            if set(record) != {
                "prev",
                "digest",
                "entry",
            }:
                raise RuntimeError(
                    f"FAIL-CLOSED: Audit-Schema Zeile {line_no}"
                )

            if record["prev"] != previous:
                raise RuntimeError(
                    f"FAIL-CLOSED: Audit-Kette Zeile {line_no}"
                )

            expected = audit_digest(
                record["entry"],
                previous,
            )

            if record["digest"] != expected:
                raise RuntimeError(
                    f"FAIL-CLOSED: Audit-Hash Zeile {line_no}"
                )

            previous = record["digest"]

    if read_audit_head() != previous:
        raise RuntimeError(
            "FAIL-CLOSED: Audit-Head stimmt nicht."
        )

    return True


# =========================================================
# PATH POLICY
# =========================================================

def resolve_runtime_path(path_text):
    if not isinstance(
        path_text,
        str,
    ):
        raise PermissionError(
            "path muss String sein."
        )

    if not path_text.strip():
        raise PermissionError(
            "Leerer Pfad."
        )

    candidate = (
        RUNTIME / path_text
    ).resolve(strict=False)

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
# TOOLS
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
    target = resolve_runtime_path(
        path
    )

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
    if not isinstance(
        content,
        str,
    ):
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

    target = resolve_runtime_path(
        path
    )

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

    if not isinstance(
        args,
        list,
    ):
        raise TypeError(
            "args muss Liste sein."
        )

    if len(args) > MAX_SCRIPT_ARGS:
        raise PermissionError(
            "Zu viele Script-Argumente."
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

    relative_script = (
        script_path
        .relative_to(RUNTIME)
        .as_posix()
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
        "--cap-drop",
        "ALL",

        "--ro-bind",
        "/usr",
        "/usr",

        "--ro-bind",
        "/bin",
        "/bin",

        "--ro-bind",
        "/lib",
        "/lib",

        "--ro-bind",
        "/lib64",
        "/lib64",

        "--ro-bind",
        str(ROOT),
        "/agent",

        "--bind",
        str(RUNTIME),
        "/workspace",

        "--proc",
        "/proc",

        "--dev",
        "/dev",

        "--tmpfs",
        "/etc",

        "--tmpfs",
        "/tmp",

        "--tmpfs",
        "/var",

        "--chdir",
        "/workspace",

        sys.executable,
        "/workspace/" + relative_script,
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

    if not isinstance(
        arguments,
        dict,
    ):
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
        allowed = {
            "script",
            "args",
        }

        if set(arguments) - allowed:
            raise PermissionError(
                "run_python enthält unerlaubte Argumente."
            )

        return run_python(
            arguments["script"],
            arguments.get(
                "args",
                [],
            ),
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

    if not isinstance(
        message,
        dict,
    ):
        raise RuntimeError(
            "Ollama-Antwort enthält keine message."
        )

    content = message.get("content")

    if not isinstance(
        content,
        str,
    ):
        raise RuntimeError(
            "Ollama-Antwort enthält keinen Text."
        )

    return content.strip()


# =========================================================
# PROTOCOL
# =========================================================


def _normalize_model_json(text):
    if not isinstance(text, str):
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort muss Text sein."
        )

    value = text.strip()

    # Genau ein vollständiger Markdown-Codeblock ist erlaubt.
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()

        if len(lines) < 3:
            raise RuntimeError(
                "FAIL-CLOSED: Ungültiger Markdown-Codeblock."
            )

        opening = lines[0].strip().lower()
        closing = lines[-1].strip()

        if closing != "```":
            raise RuntimeError(
                "FAIL-CLOSED: Codeblock nicht korrekt geschlossen."
            )

        if opening not in {
            "```",
            "```json",
        }:
            raise RuntimeError(
                "FAIL-CLOSED: Nur JSON-Codeblock erlaubt."
            )

        value = "\n".join(
            lines[1:-1]
        ).strip()

    if not value:
        raise RuntimeError(
            "FAIL-CLOSED: Leere Modellantwort."
        )

    return value



def _normalize_model_json(text):
    if not isinstance(text, str):
        raise RuntimeError(
            "FAIL-CLOSED: Modellantwort muss Text sein."
        )

    value = text.strip()

    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()

        if len(lines) < 3:
            raise RuntimeError(
                "FAIL-CLOSED: Ungültiger Markdown-Codeblock."
            )

        opening = lines[0].strip().lower()
        closing = lines[-1].strip()

        if closing != "```":
            raise RuntimeError(
                "FAIL-CLOSED: Codeblock nicht korrekt geschlossen."
            )

        if opening not in {
            "```",
            "```json",
        }:
            raise RuntimeError(
                "FAIL-CLOSED: Nur JSON-Codeblock erlaubt."
            )

        value = "\n".join(
            lines[1:-1]
        ).strip()

    if not value:
        raise RuntimeError(
            "FAIL-CLOSED: Leere Modellantwort."
        )

    return value


def parse_action(text):
    value = _normalize_model_json(text)

    try:
        obj = json.loads(value)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FAIL-CLOSED: Antwort ist kein einzelnes JSON."
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: JSON-Objekt erwartet."
        )

    # Primärformat:
    # {"action":"tool","name":"write_file","arguments":{...}}
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

    # Kurzformat des Qwen-Modells:
    # {"action":"write_file","arguments":{...}}
    if set(obj) == {
        "action",
        "arguments",
    }:
        if obj["action"] not in TOOL_NAMES:
            raise RuntimeError(
                "FAIL-CLOSED: Unbekannte Kurzformat-Aktion."
            )

        if not isinstance(
            obj["arguments"],
            dict,
        ):
            raise RuntimeError(
                "FAIL-CLOSED: arguments ungültig."
            )

        return {
            "action": "tool",
            "name": obj["action"],
            "arguments": obj["arguments"],
        }

    # Abschluss
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
# AGENT LOOP
# =========================================================

def run_agent(task):
    state = load_state()

    verify_audit_chain()

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

{"action":"tool","name":"list_files","arguments":{}}

{"action":"tool","name":"read_file","arguments":{"path":"datei.txt"}}

{"action":"tool","name":"write_file","arguments":{"path":"datei.txt","content":"TEXT"}}

{"action":"tool","name":"run_python","arguments":{"script":"test.py","args":[]}}

Abschluss:

{"action":"done","result":"..."}

Tool-Kurzformat ist ebenfalls gültig:

{"action":"write_file","arguments":{"path":"datei.txt","content":"TEXT"}}

Regeln:

- Keine Shell-Kommandos.
- Kein bash.
- Kein sh.
- Kein exec.
- Kein subprocess.
- Genau EIN JSON-Objekt pro Antwort.
- Ein einzelner ```json-Codeblock ist erlaubt.
- Kein Text vor oder nach dem JSON.
- Nur /workspace verändern.
- Niemals /agent verändern.
- Prüfe jedes Tool-Ergebnis.
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
                "status": (
                    result.get(
                        "status",
                        "OK",
                    )
                    if isinstance(
                        result,
                        dict,
                    )
                    else "OK"
                ),
            },
        )

        print("RESULT:")
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
            if isinstance(
                result,
                (dict, list),
            )
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
            'Usage: python3 safe_agent_v43.py "AUFGABE"'
        )
        raise SystemExit(2)

    print("=== SAFE AGENT V43 ===")
    print("Project:", ROOT)
    print("Runtime:", RUNTIME)
    print("Model:", MODEL)

    run_agent(
        " ".join(sys.argv[1:])
    )


if __name__ == "__main__":
    main()
PY


cat > tests/test_v43.py <<'PY'
import json
from pathlib import Path

import safe_agent_v43 as agent


ROOT = agent.ROOT
RUNTIME = agent.RUNTIME


def expect_blocked(label, fn):
    print("[TEST]", label)

    try:
        fn()
    except Exception as exc:
        print(
            "       BLOCKED:",
            type(exc).__name__,
        )
        return

    raise AssertionError(
        f"{label}: Angriff wurde akzeptiert"
    )


def test_schema():
    agent.validate_state({
        "version": 43,
        "tasks_completed": 0,
        "last_result": None,
    })

    expect_blocked(
        "Negative Tasks",
        lambda: agent.validate_state({
            "version": 43,
            "tasks_completed": -1,
            "last_result": None,
        }),
    )


def test_tool_allowlist():
    expect_blocked(
        "Unknown Tool",
        lambda: agent.execute_tool(
            "shell",
            {},
        ),
    )


def test_path_escape():
    for path in [
        "../escape.txt",
        "../../escape.txt",
        "/etc/shadow",
    ]:
        expect_blocked(
            f"Path Escape {path}",
            lambda p=path: agent.write_file(
                p,
                "NOPE",
            ),
        )


def test_write_read():
    target = "v43_rw.txt"

    agent.write_file(
        target,
        "V43_OK",
    )

    assert (
        agent.read_file(target)
        == "V43_OK"
    )

    (RUNTIME / target).unlink()


def test_content_limit():
    expect_blocked(
        "Content Limit",
        lambda: agent.write_file(
            "too_big.txt",
            "X" * (
                agent.MAX_CONTENT_BYTES + 1
            ),
        ),
    )


def test_argument_limit():
    script = RUNTIME / "arg_test.py"

    agent.write_file(
        "arg_test.py",
        "print('ARGS_OK')\n",
    )

    expect_blocked(
        "Too Many Args",
        lambda: agent.run_python(
            "arg_test.py",
            ["x"] * (
                agent.MAX_SCRIPT_ARGS + 1
            ),
        ),
    )

    script.unlink()


def test_python_execution():
    agent.write_file(
        "exec_test.py",
        "print('V43_EXEC_OK')\n",
    )

    result = agent.run_python(
        "exec_test.py",
    )

    assert result["exit_code"] == 0
    assert "V43_EXEC_OK" in result["stdout"]

    (RUNTIME / "exec_test.py").unlink()


def test_python_escape():
    agent.write_file(
        "escape_probe.py",
        (
            "from pathlib import Path\n"
            "print(Path('/var/home/mklein').exists())\n"
            "print(Path('/etc/shadow').exists())\n"
            "print(Path('/sys').exists())\n"
        ),
    )

    result = agent.run_python(
        "escape_probe.py",
    )

    assert result["exit_code"] == 0
    assert result["stdout"].splitlines() == [
        "False",
        "False",
        "False",
    ]

    (RUNTIME / "escape_probe.py").unlink()




def test_parser():
    direct = json.dumps({
        "action": "done",
        "result": "OK",
    })

    parsed = agent.parse_action(
        direct
    )

    assert parsed["action"] == "done"


    fenced = (
        "```json\n"
        '{"action":"done","result":"FENCED_OK"}'
        "\n```"
    )

    parsed = agent.parse_action(
        fenced
    )

    assert parsed["result"] == "FENCED_OK"


    shorthand = json.dumps({
        "action": "write_file",
        "arguments": {
            "path": "x.txt",
            "content": "X",
        },
    })

    parsed = agent.parse_action(
        shorthand
    )

    assert parsed == {
        "action": "tool",
        "name": "write_file",
        "arguments": {
            "path": "x.txt",
            "content": "X",
        },
    }


    expect_blocked(
        "Multiple JSON Objects",
        lambda: agent.parse_action(
            '{"action":"done","result":"A"}\n'
            '{"action":"done","result":"B"}'
        ),
    )


    expect_blocked(
        "JSON plus prose",
        lambda: agent.parse_action(
            'Hier ist die Antwort:\n'
            '{"action":"done","result":"BAD"}'
        ),
    )


    expect_blocked(
        "Unknown shorthand action",
        lambda: agent.parse_action(
            '{"action":"shell","arguments":{}}'
        ),
    )


    expect_blocked(
        "Two fenced blocks",
        lambda: agent.parse_action(
            '```json\n'
            '{"action":"done","result":"A"}\n'
            '```\n'
            '```json\n'
            '{"action":"done","result":"B"}\n'
            '```'
        ),
    )


def test_audit_chain():
    agent.audit(
        "TEST_EVENT",
        {
            "ok": True,
        },
    )

    assert agent.verify_audit_chain()


def test_agent_code_present():
    assert (
        ROOT / "safe_agent_v43.py"
    ).is_file()


if __name__ == "__main__":
    tests = [
        test_schema,
        test_tool_allowlist,
        test_path_escape,
        test_write_read,
        test_content_limit,
        test_argument_limit,
        test_python_execution,
        test_python_escape,
        test_parser,
        test_audit_chain,
        test_agent_code_present,
    ]

    for test in tests:
        test()

    print()
    print("V43 TESTS OK")
PY


cat > tools/test_all_v43.sh <<'BASH2'
#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

echo "=== COMPILE ==="
python3 -m py_compile safe_agent_v43.py

echo
echo "=== SECURITY TESTS ==="
python3 tests/test_v43.py

echo
echo "=== STATE ==="

python3 - <<'PY'
import safe_agent_v43 as agent

state = agent.load_state()

agent.verify_state_hash(
    state
)

agent.verify_audit_chain()

print(state)
print("STATE OK")
print("AUDIT CHAIN OK")
PY

echo
echo "=== ALL V43 TESTS PASSED ==="
BASH2

chmod +x tools/test_all_v43.sh


cat > tools/test_end_to_end_v43.sh <<'BASH3'
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
BASH3

chmod +x tools/test_end_to_end_v43.sh


cat > BUILD_INFO_V43.txt <<EOF
SAFE AGENT V43

Built: $(date -Is)
Model: ${OLLAMA_MODEL}
Ollama URL: ${OLLAMA_URL}

Tools:
- list_files
- read_file
- write_file
- run_python

Security:
- direct tools
- strict path validation
- content limit
- argument limit
- isolated Python execution
- network namespace disabled
- capabilities dropped
- agent code read-only
- audit hash chain
EOF


python3 -m py_compile safe_agent_v43.py

echo
echo "=== V43 BUILD ==="
./tools/test_all_v43.sh

echo
echo "=== V43 END-TO-END ==="
./tools/test_end_to_end_v43.sh

echo
echo "=========================================="
echo " SAFE AGENT V43 READY"
echo "=========================================="
