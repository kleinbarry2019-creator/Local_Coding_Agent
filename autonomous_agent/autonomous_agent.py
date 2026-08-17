#!/usr/bin/env python3

import json
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5-coder:14b"

WORKSPACE = Path(
    "/var/home/mklein/aider_workspace/autonomous_agent"
).resolve()


def getcwd() -> str:
    return str(Path.cwd())


def list_files() -> str:
    items = [path.name for path in sorted(WORKSPACE.iterdir())]
    return "\n".join(items) if items else "(leer)"


def read_file(path: str) -> str:
    target = (WORKSPACE / path).resolve()

    if WORKSPACE not in target.parents and target != WORKSPACE:
        raise ValueError(
            "Zugriff außerhalb des Agent-Workspace verweigert."
        )

    if not target.is_file():
        raise FileNotFoundError(path)

    return target.read_text(encoding="utf-8")


def write_file(path: str, content: str) -> str:
    target = (WORKSPACE / path).resolve()

    if WORKSPACE not in target.parents and target != WORKSPACE:
        raise ValueError(
            "Zugriff außerhalb des Agent-Workspace verweigert."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return f"Datei geschrieben: {target}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getcwd",
            "description": "Gibt das aktuelle Arbeitsverzeichnis zurück.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Listet Dateien im Agent-Workspace auf.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Liest eine Datei aus dem Agent-Workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad innerhalb des Workspace.",
                    }
                },
                "required": ["path"],
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
                    "path": {
                        "type": "string",
                        "description": "Relativer Pfad innerhalb des Workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Gesamter Dateiinhalt.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
]


def call_ollama(messages: list[dict]) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
    }

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_tool_calls(message: dict) -> tuple[dict, list[dict]]:
    content = message.get("content", "")
    tool_calls = message.get("tool_calls", [])

    # Manche Ollama/Qwen-Antworten liefern den Tool-Call
    # als JSON-String in message.content.
    if not tool_calls and content:
        try:
            parsed = json.loads(content)

            if (
                isinstance(parsed, dict)
                and parsed.get("name")
                and "arguments" in parsed
            ):
                tool_calls = [
                    {
                        "function": {
                            "name": parsed["name"],
                            "arguments": parsed["arguments"],
                        }
                    }
                ]

                message = {
                    "role": "assistant",
                    "tool_calls": tool_calls,
                }

                content = ""

        except json.JSONDecodeError:
            pass

    message = dict(message)
    message["_display_content"] = content

    return message, tool_calls


def execute_tool(name: str, arguments: dict) -> str:
    if name == "getcwd":
        return getcwd()

    if name == "list_files":
        return list_files()

    if name == "read_file":
        return read_file(arguments["path"])

    if name == "write_file":
        return write_file(
            arguments["path"],
            arguments["content"],
        )

    return f"Unbekanntes Tool: {name}"


def main() -> None:
    print(f"Agent Workspace: {WORKSPACE}")
    print(f"Model: {MODEL}")
    print()

    messages = [
        {
            "role": "system",
            "content": (
                "Du bist ein autonomer Coding-Agent. "
                "Arbeite selbstständig und nutze Tools, wenn sie benötigt werden. "
                "Arbeite ausschließlich innerhalb des Agent-Workspace. "
                "Erledige die Aufgabe vollständig."
            ),
        },
        {
            "role": "user",
            "content": (
                "Untersuche deinen Workspace. "
                "Erstelle hello.py mit exakt folgendem Inhalt: "
                "print('Autonomous agent OK'). "
                "Prüfe danach selbstständig, ob die Datei existiert "
                "und lies ihren Inhalt wieder ein."
            ),
        },
    ]

    for step in range(10):
        print(f"--- Agent-Schritt {step + 1} ---")

        result = call_ollama(messages)
        raw_message = result["message"]

        message, tool_calls = normalize_tool_calls(raw_message)
        content = message.pop("_display_content", "")

        if content:
            print(content)

        if not tool_calls:
            print("\nAgent beendet.")
            break

        messages.append(message)

        for tool_call in tool_calls:
            function = tool_call["function"]
            name = function["name"]
            arguments = function.get("arguments", {})

            print(f"[TOOL] {name} {arguments}")

            try:
                output = execute_tool(name, arguments)
            except Exception as exc:
                output = (
                    f"FEHLER: {type(exc).__name__}: {exc}"
                )

            print(f"[RESULT] {output}")

            messages.append(
                {
                    "role": "tool",
                    "content": output,
                }
            )


if __name__ == "__main__":
    main()
