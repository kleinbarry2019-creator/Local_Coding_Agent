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


def getcwd():
    return str(WORKSPACE)


def list_files():
    return sorted(p.name for p in WORKSPACE.iterdir())


def write_file(path: str, content: str):
    if not isinstance(path, str):
        raise TypeError("path muss ein String sein")

    if not isinstance(content, str):
        raise TypeError("content muss ein String sein")

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
        return False, f"Tool nicht freigegeben: {name}"

    if not isinstance(arguments, dict):
        return False, "Argumente sind kein Objekt."

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
            return False, "write_file.path fehlt oder ist ungültig."

        if not isinstance(content, str):
            return False, "write_file.content fehlt oder ist ungültig."

        target = (WORKSPACE / path).resolve()

        try:
            target.relative_to(WORKSPACE)
        except ValueError:
            return False, "write_file außerhalb der Sandbox."

        if target == WORKSPACE:
            return False, "Workspace selbst darf nicht überschrieben werden."

    return True, "ALLOW"


def execute_tool(name, arguments):
    allowed, reason = policy_check(name, arguments)

    print(f"POLICY: {name} -> {reason}")

    if not allowed:
        raise PermissionError(reason)

    if name == "getcwd":
        return getcwd()

    if name == "list_files":
        return list_files()

    if name == "write_file":
        return write_file(
            arguments["path"],
            arguments["content"],
        )

    raise RuntimeError(f"Unbekanntes Tool: {name}")


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
            "description": "Schreibt genau eine Datei innerhalb des Agent-Workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad innerhalb des Workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Der vollständige Dateiinhalt.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
]


def call_model(user_prompt):
    system_prompt = """Du bist ein Tool-Calling-Agent.

WICHTIG:
- Antworte niemals mit Python-Code.
- Antworte niemals mit Erklärungen.
- Antworte ausschließlich mit genau EINEM JSON-Objekt.
- Das JSON-Objekt MUSS exakt dieses Format haben:
  {"name":"TOOLNAME","arguments":{...}}
- Erlaubte Tools sind ausschließlich:
  getcwd
  list_files
  write_file
- Bei write_file müssen path und content angegeben werden.
- Führe niemals selbst Tools aus.
"""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
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


def parse_single_tool_call(content):
    text = content.strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Modell hat keinen gültigen JSON-Tool-Call geliefert:\n"
            + text
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError("Tool-Call ist kein JSON-Objekt.")

    if set(obj.keys()) != {"name", "arguments"}:
        raise RuntimeError(
            "Tool-Call muss exakt 'name' und 'arguments' enthalten."
        )

    if not isinstance(obj["name"], str):
        raise RuntimeError("Tool-Name ist kein String.")

    if not isinstance(obj["arguments"], dict):
        raise RuntimeError("Tool-Argumente sind kein Objekt.")

    return obj["name"], obj["arguments"]


def main():
    print("=== SAFE AGENT V9 ===")
    print("Workspace:", WORKSPACE)

    prompt = (
        "Schreibe exakt einmal mit write_file die Datei "
        "agent_v9_test.txt mit dem Inhalt "
        "\"V9 STRUCTURED POLICY OK\"."
    )

    result = call_model(prompt)
    message = result.get("message", {})
    content = message.get("content", "")

    print("Model:")
    print(content)

    name, arguments = parse_single_tool_call(content)

    print()
    print("Tool:", name)
    print("Arguments:", arguments)

    result = execute_tool(name, arguments)

    print("Result:", result)

    target = WORKSPACE / "agent_v9_test.txt"

    if not target.exists():
        raise RuntimeError("Testdatei wurde nicht erzeugt.")

    actual = target.read_text(encoding="utf-8")

    if actual != "V9 STRUCTURED POLICY OK":
        raise RuntimeError(
            f"Falscher Inhalt: {actual!r}"
        )

    print()
    print("SAFE AGENT V9 OK")


if __name__ == "__main__":
    main()
