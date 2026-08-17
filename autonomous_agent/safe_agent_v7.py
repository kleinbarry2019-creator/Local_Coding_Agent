import json
import os
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()
ALLOWED_TOOLS = {"getcwd", "list_files", "write_file"}


def getcwd():
    return str(WORKSPACE)


def list_files():
    return sorted(
        p.name for p in WORKSPACE.iterdir()
    )


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
            "Der Workspace selbst darf nicht überschrieben werden."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return {
        "path": str(target),
        "bytes": len(content.encode("utf-8")),
    }


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getcwd",
            "description": "Gibt das aktuelle Arbeitsverzeichnis zurück.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Listet Dateien im aktuellen Arbeitsverzeichnis auf.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Schreibt eine Datei innerhalb des Agent-Workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string"
                    },
                    "content": {
                        "type": "string"
                    }
                },
                "required": ["path", "content"]
            }
        }
    }
]


def call_model(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "options": {
            "num_ctx": 1024,
            "temperature": 0
        },
        "stream": False
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def parse_tool_calls(content):
    calls = []

    for line in content.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(obj, dict) and "name" in obj:
            calls.append(obj)

    return calls


def execute_tool(name, arguments):
    if name not in ALLOWED_TOOLS:
        raise PermissionError(f"Tool nicht erlaubt: {name}")

    if not isinstance(arguments, dict):
        raise TypeError("Tool-Argumente müssen ein Objekt sein.")

    if name == "getcwd":
        return getcwd()

    if name == "list_files":
        return list_files()

    if name == "write_file":
        return write_file(
            arguments.get("path"),
            arguments.get("content"),
        )

    raise RuntimeError(f"Unbekanntes Tool: {name}")


def run():
    messages = [
        {
            "role": "user",
            "content": (
                "Schreibe exakt einmal mit write_file die Datei "
                "agent_v7_test.txt mit dem Inhalt "
                "\"V7 WRITE OK\". "
                "Verwende keine anderen Tools."
            )
        }
    ]

    result = call_model(messages)
    content = result["message"].get("content", "").strip()

    print("Model:")
    print(content)

    calls = parse_tool_calls(content)

    if not calls:
        raise RuntimeError("Modell hat keinen Tool-Call geliefert.")

    executed = []

    for call in calls:
        name = call.get("name")
        arguments = call.get("arguments", {})

        print()
        print("Tool:", name)
        print("Arguments:", arguments)

        result = execute_tool(name, arguments)
        executed.append(name)

        print("Result:", result)

    return executed


if __name__ == "__main__":
    print("=== SAFE AGENT V7 ===")
    print("Workspace:", WORKSPACE)

    executed = run()

    print()
    print("Executed:", executed)

    if executed != ["write_file"]:
        raise RuntimeError(
            f"Unerwartete Tool-Sequenz: {executed}"
        )

    target = WORKSPACE / "agent_v7_test.txt"

    if not target.exists():
        raise RuntimeError("Testdatei wurde nicht erzeugt.")

    if target.read_text(encoding="utf-8") != "V7 WRITE OK":
        raise RuntimeError("Testdatei enthält falschen Inhalt.")

    print("SAFE AGENT V7 OK")
