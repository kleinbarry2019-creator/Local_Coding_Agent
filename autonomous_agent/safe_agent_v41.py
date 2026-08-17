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
