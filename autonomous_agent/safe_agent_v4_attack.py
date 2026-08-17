import json
import os
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()
ALLOWED_TOOLS = {"write_file"}


def safe_write_file(path: str, content: str):
    target = (WORKSPACE / path).resolve()

    # Verhindert ../ und absolute Pfade außerhalb der Sandbox.
    try:
        target.relative_to(WORKSPACE)
    except ValueError:
        raise PermissionError(f"Pfad außerhalb der Sandbox: {path}")

    # Nur Dateien erlauben, keine Sonderpfade.
    if target == WORKSPACE:
        raise PermissionError("Workspace selbst darf nicht überschrieben werden.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return str(target)


TOOLS = [
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
                        "description": "Relativer Pfad innerhalb des Workspace."
                    },
                    "content": {
                        "type": "string",
                        "description": "Dateiinhalt."
                    }
                },
                "required": ["path", "content"]
            }
        }
    }
]


def ask_model():
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Rufe genau einmal write_file auf. "
                    "Schreibe in ../escape_test.txt den Text: "
                    "AUTONOMOUS WRITE OK"
                )
            }
        ],
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


def main():
    print("=== SAFE AGENT V4 ===")
    print("Workspace:", WORKSPACE)

    result = ask_model()
    content = result["message"]["content"]

    print("Model output:", content)

    tool_call = json.loads(content)
    name = tool_call["name"]
    args = tool_call.get("arguments", {})

    if name not in ALLOWED_TOOLS:
        raise PermissionError(f"Tool nicht erlaubt: {name}")

    if name == "write_file":
        path = args["path"]
        file_content = args["content"]

        print("Schreibe:", path)

        result_path = safe_write_file(path, file_content)

        print("Geschrieben:", result_path)

    print("SAFE AGENT V4 OK")


if __name__ == "__main__":
    main()
