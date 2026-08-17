import json
import os
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()

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


def write_file(path, content):
    if not isinstance(path, str):
        raise TypeError("path muss String sein")

    if not isinstance(content, str):
        raise TypeError("content muss String sein")

    target = (WORKSPACE / path).resolve()

    try:
        target.relative_to(WORKSPACE)
    except ValueError:
        raise PermissionError(
            f"Pfad außerhalb der Sandbox: {path}"
        )

    if target == WORKSPACE:
        raise PermissionError(
            "Workspace selbst darf nicht überschrieben werden."
        )

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

        target = (WORKSPACE / path).resolve()

        try:
            target.relative_to(WORKSPACE)
        except ValueError:
            return False, "write_file außerhalb der Sandbox."

        if target == WORKSPACE:
            return False, "Workspace darf nicht überschrieben werden."

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
    system_prompt = """Du bist ein strikter Tool-Calling-Agent.

Regeln:
- Antworte mit GENAU EINEM JSON-Objekt.
- Kein Markdown.
- Kein Python.
- Keine Erklärungen.
- Kein zusätzlicher Text.
- Entweder ein Tool-Call:
  {"name":"TOOLNAME","arguments":{...}}
- oder exakt:
  {"final":"TEXT"}
- Niemals mehrere JSON-Objekte in einer Antwort.
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
            "FAIL-CLOSED: Antwort ist kein einzelnes gültiges JSON:\n"
            + text
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: Antwort ist kein JSON-Objekt."
        )

    if "name" in obj and "arguments" in obj:
        if set(obj) != {"name", "arguments"}:
            raise RuntimeError(
                "FAIL-CLOSED: Tool-Call enthält unerlaubte Felder."
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
                "FAIL-CLOSED: Final-Antwort enthält unerlaubte Felder."
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
        "FAIL-CLOSED: Unbekanntes Antwortschema."
    )


def main():
    print("=== SAFE AGENT V12 ===")
    print("Workspace:", WORKSPACE)

    messages = [
        {
            "role": "user",
            "content": (
                "Führe genau EINEN Schritt aus: "
                "Rufe ausschließlich getcwd auf."
            ),
        }
    ]

    result = call_model(messages)
    content = result.get("message", {}).get("content", "")

    print("MODEL:")
    print(content)

    parsed = parse_response(content)

    if parsed["type"] == "final":
        print("FINAL:", parsed["content"])
        print("SAFE AGENT V12 OK")
        return

    name = parsed["name"]
    arguments = parsed["arguments"]

    print("TOOL:", name)
    print("ARGUMENTS:", arguments)

    result = execute_tool(name, arguments)

    print("RESULT:", result)
    print("SAFE AGENT V12 OK")


if __name__ == "__main__":
    main()
