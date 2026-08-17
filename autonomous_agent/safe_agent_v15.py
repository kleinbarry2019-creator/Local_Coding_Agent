import json
import subprocess
import sys
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()

MAX_MODEL_STEPS = 5
MAX_TOOL_CALLS = 2
COMMAND_TIMEOUT = 10

ALLOWED_TOOLS = {
    "run_python",
}

# Nur diese Skripte dürfen über das Tool gestartet werden.
ALLOWED_SCRIPTS = {
    "safe_test.py",
}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Führt genau ein freigegebenes Python-Skript "
                "innerhalb des Agent-Workspace aus."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "Relativer Name eines freigegebenen Python-Skripts.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optionale Argumente.",
                    },
                },
                "required": ["script"],
                "additionalProperties": False,
            },
        },
    },
]


def resolve_safe_script(script: str) -> Path:
    if not isinstance(script, str):
        raise TypeError("script muss ein String sein")

    if not script:
        raise PermissionError("Leerer Scriptname ist nicht erlaubt.")

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
        raise PermissionError(
            "Nur Python-Skripte sind erlaubt."
        )

    if not target.is_file():
        raise FileNotFoundError(
            f"Skript nicht gefunden: {script}"
        )

    return target


def run_python(script, args=None):
    target = resolve_safe_script(script)

    if args is None:
        args = []

    if not isinstance(args, list):
        raise TypeError("args muss eine Liste sein")

    if not all(isinstance(arg, str) for arg in args):
        raise TypeError("Alle args müssen Strings sein")

    # WICHTIG:
    # - shell=False
    # - argv als Liste
    # - cwd bleibt im Workspace
    # - Timeout begrenzt Hänger
    command = [
        sys.executable,
        str(target),
        *args,
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
        return False, "Argumente müssen Objekt sein."

    if name == "run_python":
        script = arguments.get("script")
        args = arguments.get("args", [])

        if not isinstance(script, str):
            return False, "run_python.script ungültig."

        if not isinstance(args, list):
            return False, "run_python.args muss Liste sein."

        if not all(isinstance(arg, str) for arg in args):
            return False, "run_python.args darf nur Strings enthalten."

        try:
            resolve_safe_script(script)
        except (PermissionError, TypeError, FileNotFoundError) as exc:
            return False, str(exc)

    return True, "ALLOW"


def execute_tool(name, arguments):
    allowed, reason = policy_check(name, arguments)

    print(f"POLICY: {name} -> {reason}")

    if not allowed:
        raise PermissionError(reason)

    if name == "run_python":
        return run_python(
            arguments["script"],
            arguments.get("args", []),
        )

    raise RuntimeError(f"Unbekanntes Tool: {name}")


def call_model(messages):
    system_prompt = """Du bist ein strikt kontrollierter Test-Agent.

Antworte IMMER mit GENAU EINEM JSON-Objekt.

Tool:
{"name":"run_python","arguments":{"script":"safe_test.py"}}

Final:
{"final":"TEXT"}

Regeln:
- Niemals mehrere JSON-Objekte.
- Kein Markdown.
- Kein Python-Code.
- Kein Shell-Code.
- Kein exec.
- Kein bash.
- Kein sh.
- Nur das bereitgestellte Tool verwenden.
- Nur freigegebene Skripte verwenden.
- Pro Antwort höchstens EIN Tool.
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
        "tools": TOOLS,
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
            "FAIL-CLOSED: Antwort ist kein einzelnes gültiges JSON."
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: Antwort ist kein JSON-Objekt."
        )

    if "name" in obj or "arguments" in obj:
        if set(obj) != {"name", "arguments"}:
            raise RuntimeError(
                "FAIL-CLOSED: ungültiges Tool-Schema."
            )

        if not isinstance(obj["name"], str):
            raise RuntimeError(
                "FAIL-CLOSED: Toolname muss String sein."
            )

        if not isinstance(obj["arguments"], dict):
            raise RuntimeError(
                "FAIL-CLOSED: arguments muss Objekt sein."
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
                "FAIL-CLOSED: final muss String sein."
            )

        return {
            "type": "final",
            "content": obj["final"],
        }

    raise RuntimeError(
        "FAIL-CLOSED: unbekanntes Antwortschema."
    )


def main():
    print("=== SAFE AGENT V15 ===")
    print("Workspace:", WORKSPACE)
    print("Allowed scripts:", sorted(ALLOWED_SCRIPTS))

    messages = [
        {
            "role": "user",
            "content": (
                "Führe genau einmal das freigegebene Skript "
                "\"safe_test.py\" aus. "
                "Danach gib eine kurze Zusammenfassung."
            ),
        }
    ]

    used_tools = set()

    for step in range(1, MAX_MODEL_STEPS + 1):
        print(
            f"\n--- MODEL STEP {step}/{MAX_MODEL_STEPS} ---"
        )

        result = call_model(messages)
        content = result.get("message", {}).get("content", "")

        print("MODEL:")
        print(content)

        parsed = parse_response(content)

        if parsed["type"] == "final":
            print("\nFINAL:")
            print(parsed["content"])
            print("\nSAFE AGENT V15 OK")
            return

        name = parsed["name"]
        arguments = parsed["arguments"]

        if name in used_tools:
            raise RuntimeError(
                f"Tool erneut angefordert: {name}"
            )

        if len(used_tools) >= MAX_TOOL_CALLS:
            raise RuntimeError(
                "FAIL-CLOSED: Tool-Budget erschöpft."
            )

        result_data = execute_tool(name, arguments)

        used_tools.add(name)

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
                    + "\n"
                    "Das Tool wurde bereits ausgeführt. "
                    "Fordere es nicht erneut an."
                ),
            }
        )

    raise RuntimeError(
        "FAIL-CLOSED: maximales Modellschritt-Limit erreicht."
    )


if __name__ == "__main__":
    main()
