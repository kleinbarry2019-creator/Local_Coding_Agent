import json
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()

MAX_MODEL_STEPS = 6
MAX_TOOL_CALLS = 3

ALLOWED_TOOLS = {
    "getcwd",
    "list_files",
    "write_file",
}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getcwd",
            "description": "Gibt das aktuelle Arbeitsverzeichnis zurück.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Listet Dateien im aktuellen Arbeitsverzeichnis auf.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Schreibt eine Datei innerhalb des Agent-Workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]


def getcwd():
    return str(WORKSPACE)


def list_files():
    return sorted(p.name for p in WORKSPACE.iterdir())


def resolve_safe_target(path: str) -> Path:
    if not isinstance(path, str):
        raise TypeError("path muss String sein")

    if not path:
        raise PermissionError("Leerer Pfad ist nicht erlaubt.")

    candidate = WORKSPACE / path

    # Alle vorhandenen Bestandteile einschließlich Symlinks auflösen.
    target = candidate.resolve(strict=False)

    try:
        target.relative_to(WORKSPACE)
    except ValueError as exc:
        raise PermissionError(
            f"Pfad außerhalb der Sandbox: {path}"
        ) from exc

    if target == WORKSPACE:
        raise PermissionError(
            "Workspace selbst darf nicht überschrieben werden."
        )

    return target


def write_file(path, content):
    if not isinstance(content, str):
        raise TypeError("content muss String sein")

    target = resolve_safe_target(path)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return {
        "path": str(target),
        "bytes": len(content.encode("utf-8")),
    }


def policy_check(name, arguments):
    if name not in ALLOWED_TOOLS:
        return False, f"Tool nicht erlaubt: {name}"

    if not isinstance(arguments, dict):
        return False, "Argumente müssen Objekt sein."

    if name == "getcwd":
        if arguments:
            return False, "getcwd akzeptiert keine Argumente."

    elif name == "list_files":
        if arguments:
            return False, "list_files akzeptiert keine Argumente."

    elif name == "write_file":
        path = arguments.get("path")
        content = arguments.get("content")

        if not isinstance(path, str):
            return False, "write_file.path ungültig."

        if not isinstance(content, str):
            return False, "write_file.content ungültig."

        try:
            resolve_safe_target(path)
        except (PermissionError, TypeError) as exc:
            return False, str(exc)

    return True, "ALLOW"


def execute_tool(name, arguments):
    allowed, reason = policy_check(name, arguments)

    print(f"POLICY: {name} -> {reason}")

    if not allowed:
        raise PermissionError(reason)

    if name == "getcwd":
        return {"cwd": getcwd()}

    if name == "list_files":
        return {"files": list_files()}

    if name == "write_file":
        return write_file(
            arguments["path"],
            arguments["content"],
        )

    raise RuntimeError(f"Unbekanntes Tool: {name}")


def call_model(messages):
    system_prompt = """Du bist ein strikt kontrollierter Tool-Calling-Agent.

Antworte IMMER mit GENAU EINEM JSON-Objekt.

Tool:
{"name":"TOOLNAME","arguments":{...}}

Final:
{"final":"TEXT"}

Regeln:
- Niemals mehrere JSON-Objekte.
- Kein Markdown.
- Kein Python.
- Kein zusätzlicher Text.
- Fordere pro Antwort höchstens EIN Tool an.
- Fordere kein bereits ausgeführtes Tool erneut an.
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
            "FAIL-CLOSED: kein einzelnes gültiges JSON."
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
    print("=== SAFE AGENT V14 ===")
    print("Workspace:", WORKSPACE)

    messages = [
        {
            "role": "user",
            "content": (
                "Ermittle das aktuelle Arbeitsverzeichnis. "
                "Liste danach die Dateien auf. "
                "Schreibe danach exakt einmal "
                "agent_v14_test.txt mit dem Inhalt "
                "\"V14 SANDBOX OK\". "
                "Führe jedes Tool höchstens einmal aus."
            ),
        }
    ]

    used_tools = set()

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
            print("\nSAFE AGENT V14 OK")
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

        print("RESULT:", result_data)

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
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\nBereits verwendete Tools:\n"
                    + json.dumps(sorted(used_tools))
                    + "\nFordere kein bereits verwendetes Tool erneut an."
                ),
            }
        )

    raise RuntimeError(
        "FAIL-CLOSED: maximales Modellschritt-Limit erreicht."
    )


if __name__ == "__main__":
    main()
