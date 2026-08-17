import json
import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()

MAX_MODEL_STEPS = 5
MAX_TOOL_CALLS = 1

MAX_OUTPUT_CHARS = 4096
COMMAND_TIMEOUT = 10

ALLOWED_TOOLS = {"run_python_sandbox"}
ALLOWED_SCRIPTS = {"safe_test.py"}


STATE_FILE = Path("agent_state.json")
STATE_BACKUP_FILE = Path("agent_state.json.bak")
STATE_HASH_FILE = Path("agent_state.sha256")
MAX_STATE_BYTES = 16384

AUDIT_FILE = Path("audit_log.jsonl")
MAX_AUDIT_BYTES = 1048576




def audit(event, data=None):

    import json
    import time

    entry = {
        "time": time.time(),
        "event": event,
        "data": data,
    }

    if AUDIT_FILE.exists():

        if AUDIT_FILE.stat().st_size > MAX_AUDIT_BYTES:
            raise RuntimeError(
                "FAIL-CLOSED: Audit Log zu groß"
            )

    with AUDIT_FILE.open(
        "a",
        encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                entry,
                ensure_ascii=False
            )
            + "\n"
        )




def calculate_state_hash(state):

    import json
    import hashlib

    data = json.dumps(
        state,
        sort_keys=True,
        ensure_ascii=False,
    )

    return hashlib.sha256(
        data.encode("utf-8")
    ).hexdigest()



def verify_state_hash(state):

    if not STATE_HASH_FILE.exists():
        return False

    stored = STATE_HASH_FILE.read_text(
        encoding="utf-8"
    ).strip()

    return stored == calculate_state_hash(state)



def write_state_hash(state):

    digest = calculate_state_hash(state)

    STATE_HASH_FILE.write_text(
        digest,
        encoding="utf-8"
    )





def validate_state(state):

    required = {
        "version",
        "tasks_completed",
        "last_result",
    }

    if not isinstance(state, dict):
        raise RuntimeError(
            "FAIL-CLOSED: State kein Objekt"
        )

    missing = required - set(state.keys())

    if missing:
        raise RuntimeError(
            f"FAIL-CLOSED: State Felder fehlen: {missing}"
        )

    if not isinstance(state["version"], int):
        raise RuntimeError(
            "FAIL-CLOSED: version ungültig"
        )

    if not isinstance(state["tasks_completed"], int):
        raise RuntimeError(
            "FAIL-CLOSED: tasks_completed ungültig"
        )

    if state["tasks_completed"] < 0:
        raise RuntimeError(
            "FAIL-CLOSED: tasks_completed negativ"
        )

    if (
        state["last_result"] is not None
        and not isinstance(
            state["last_result"],
            str
        )
    ):
        raise RuntimeError(
            "FAIL-CLOSED: last_result ungültig"
        )




def recover_state():

    if not STATE_BACKUP_FILE.exists():
        raise RuntimeError(
            "FAIL-CLOSED: Keine State Recovery möglich"
        )

    import json

    try:
        with STATE_BACKUP_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            backup = json.load(f)

    except Exception as exc:
        raise RuntimeError(
            f"FAIL-CLOSED: Backup beschädigt: {exc}"
        )

    validate_state(backup)

    STATE_FILE.write_text(
        json.dumps(
            backup,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    audit(
        "STATE_RECOVERY",
        backup
    )

    return backup


def load_state():

    if not STATE_FILE.exists():
        return {
            "version": 1,
            "tasks_completed": 0,
            "last_result": None,
        }

    if STATE_FILE.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeError(
            "FAIL-CLOSED: State zu groß."
        )

    import json

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

    except Exception as exc:

        return recover_state()


    validate_state(data)

    if not isinstance(data, dict):
        raise RuntimeError(
            "FAIL-CLOSED: State muss Objekt sein."
        )

    return data



def save_state(state):
    validate_state(state)
    if STATE_FILE.exists():
        STATE_BACKUP_FILE.write_text(STATE_FILE.read_text(), encoding="utf-8")
    encoded = json.dumps(state, indent=2)
    STATE_FILE.write_text(encoded, encoding="utf-8")
    STATE_HASH_FILE.write_text(calculate_state_hash(state), encoding="utf-8")
    audit_log("state_saved", {"tasks_completed": state.get("tasks_completed")})


    import json

    encoded = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
    )

    if len(encoded.encode()) > MAX_STATE_BYTES:
        raise RuntimeError(
            "FAIL-CLOSED: State überschreitet Limit."
        )

    if STATE_FILE.exists():

        STATE_BACKUP_FILE.write_text(
            STATE_FILE.read_text(
                encoding="utf-8"
            ),
            encoding="utf-8"
        )


    STATE_FILE.write_text(
        encoded,
        encoding="utf-8"
    )

    write_state_hash(state)



def resolve_safe_script(script: str) -> Path:
    if not isinstance(script, str):
        raise TypeError("script muss ein String sein")

    if script not in ALLOWED_SCRIPTS:
        raise PermissionError(
            f"Skript nicht freigegeben: {script}"
        )

    target = (WORKSPACE / script).resolve(strict=False)

    try:
        target.relative_to(WORKSPACE)
    except ValueError as exc:
        raise PermissionError(
            f"Skript außerhalb der Sandbox: {script}"
        ) from exc

    if target.suffix != ".py":
        raise PermissionError("Nur Python-Skripte sind erlaubt.")

    if not target.is_file():
        raise FileNotFoundError(
            f"Skript nicht gefunden: {script}"
        )

    return target


def run_python_sandbox(script):
    target = resolve_safe_script(script)

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
        "--die-with-parent",

        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",

        "--proc", "/proc",
        "--dev", "/dev",

        "--size", "536870912",
        "--tmpfs", "/tmp",

        "--ro-bind", str(WORKSPACE), "/workspace",
        "--chdir", "/workspace",

        sys.executable,
        "/workspace/" + target.name,
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=WORKSPACE,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT,
            shell=False,
            check=False,
        )

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "TIMEOUT",
            "exit_code": None,
            "stdout": "",
            "stderr": "Sandbox timeout",
            "script": script,
        }


    stdout = completed.stdout[:MAX_OUTPUT_CHARS]
    stderr = completed.stderr[:MAX_OUTPUT_CHARS]

    return {
        "ok": completed.returncode == 0,
        "status": "COMPLETED",
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "script": script,
    }


def policy_check(name, arguments):
    if name not in ALLOWED_TOOLS:
        return False, f"Tool nicht erlaubt: {name}"

    if not isinstance(arguments, dict):
        return False, "Argumente müssen ein Objekt sein."

    if set(arguments) != {"script"}:
        return False, "Nur das Argument 'script' ist erlaubt."

    try:
        resolve_safe_script(arguments["script"])
    except (PermissionError, TypeError, FileNotFoundError) as exc:
        return False, str(exc)

    return True, "ALLOW"


def execute_tool(name, arguments):
    allowed, reason = policy_check(name, arguments)

    print(f"POLICY: {name} -> {reason}")

    if not allowed:
        raise PermissionError(reason)

    return run_python_sandbox(arguments["script"])


def call_model(messages):
    system_prompt = """Du bist ein strikt kontrollierter Test-Agent.

Antworte immer mit genau EINEM JSON-Objekt.

Tool:
{"name":"run_python_sandbox","arguments":{"script":"safe_test.py"}}

Final:
{"final":"TEXT"}

Regeln:
- Kein Markdown.
- Kein Python.
- Kein Shell-Code.
- Keine Befehle.
- Keine mehreren JSON-Objekte.
- Nur das bereitgestellte Tool.
- Nur safe_test.py darf angefordert werden.
- Höchstens ein Tool pro Antwort.
"""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            *messages,
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "run_python_sandbox",
                    "description": (
                        "Führt safe_test.py in einer isolierten "
                        "bubblewrap-Sandbox aus."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "script": {
                                "type": "string"
                            }
                        },
                        "required": ["script"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "options": {
            "num_ctx": 1024,
            "temperature": 0,
        },
        "stream": False,
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def parse_response(content):
    text = content.strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FAIL-CLOSED: ungültiges JSON."
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: JSON-Objekt erwartet."
        )

    if "name" in obj or "arguments" in obj:
        if set(obj) != {"name", "arguments"}:
            raise RuntimeError(
                "FAIL-CLOSED: ungültiges Tool-Schema."
            )

        if not isinstance(obj["name"], str):
            raise RuntimeError(
                "FAIL-CLOSED: Toolname ungültig."
            )

        if not isinstance(obj["arguments"], dict):
            raise RuntimeError(
                "FAIL-CLOSED: arguments ungültig."
            )

        return {
            "type": "tool",
            "name": obj["name"],
            "arguments": obj["arguments"],
        }

    if "final" in obj:
        if set(obj) != {"final"}:
            raise RuntimeError(
                "FAIL-CLOSED: ungültiges Final-Schema."
            )

        if not isinstance(obj["final"], str):
            raise RuntimeError(
                "FAIL-CLOSED: final ungültig."
            )

        return {
            "type": "final",
            "content": obj["final"],
        }

    raise RuntimeError(
        "FAIL-CLOSED: unbekanntes Antwortschema."
    )


def main():
    print("=== SAFE AGENT V27 ===")
    print("Workspace:", WORKSPACE)

    state = load_state()

    audit(
        "AGENT_START",
        state
    )

    print("STATE:")
    print(json.dumps(
        state,
        indent=2,
        ensure_ascii=False,
    ))

    messages = [
        {
            "role": "user",
            "content": (
                "Führe genau einmal safe_test.py "
                "in der isolierten Sandbox aus. "
                "Danach gib eine kurze Zusammenfassung."
            ),
        }
    ]

    tool_calls = 0

    for step in range(1, MAX_MODEL_STEPS + 1):
        print(f"\n--- MODEL STEP {step}/{MAX_MODEL_STEPS} ---")

        result = call_model(messages)
        content = result.get("message", {}).get("content", "")

        print("MODEL:")
        print(content)

        parsed = parse_response(content)

        if parsed["type"] == "final":
            print("\nFINAL:")
            print(parsed["content"])

            state["tasks_completed"] = (
                int(state.get("tasks_completed", 0))
                + 1
            )

            state["last_result"] = parsed["content"]

            save_state(state)

            audit(
                "TASK_COMPLETED",
                state
            )
            print("\nSAFE AGENT V27 OK")
            return

        if tool_calls >= MAX_TOOL_CALLS:
            raise RuntimeError(
                "FAIL-CLOSED: Tool-Budget erschöpft."
            )

        if tool_calls > 0:
            raise RuntimeError(
                "FAIL-CLOSED: Tool wurde erneut angefordert."
            )

        result_data = execute_tool(
            parsed["name"],
            parsed["arguments"],
        )

        tool_calls += 1

        print("RESULT:")

        safe_result = {
            "ok": bool(result_data.get("ok")),
            "status": result_data.get("status"),
            "exit_code": result_data.get("exit_code"),
            "stdout": str(result_data.get("stdout", ""))[:MAX_OUTPUT_CHARS],
            "stderr": str(result_data.get("stderr", ""))[:MAX_OUTPUT_CHARS],
        }

        print(json.dumps(
            safe_result,
            indent=2,
            ensure_ascii=False,
        ))

        messages.append(
            {
                "role": "assistant",
                "content": content,
            }
        )

        messages.append(
            {
                "role": "user",
                "content": (
                    "Tool-Ergebnis:\n"
                    + json.dumps(
                        result_data,
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\nDas Tool wurde bereits einmal ausgeführt."
                ),
            }
        )

    raise RuntimeError(
        "FAIL-CLOSED: Modellschritt-Limit erreicht."
    )


def audit_log(event, details=None):
    import json as _json
    import datetime as _dt
    entry = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event,
        "details": details or {},
    }
    with AUDIT_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry) + "\n")


if __name__ == "__main__":
    main()
