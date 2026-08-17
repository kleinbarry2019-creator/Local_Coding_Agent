import json
import subprocess
import sys
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()

MAX_MODEL_STEPS = 5
MAX_TOOL_CALLS = 1
COMMAND_TIMEOUT = 10

ALLOWED_TOOLS = {"run_python_sandbox"}
ALLOWED_SCRIPTS = {"safe_test.py"}


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

    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
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
    print("=== SAFE AGENT V16 ===")
    print("Workspace:", WORKSPACE)

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
            print("\nSAFE AGENT V16 OK")
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
        print(json.dumps(
            result_data,
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


if __name__ == "__main__":
    main()
