import json
import os
import urllib.request


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


def getcwd():
    return os.getcwd()


def list_files():
    return sorted(os.listdir("."))


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
            "description": "Listet die Dateien im aktuellen Arbeitsverzeichnis auf.",
            "parameters": {
                "type": "object",
                "properties": {}
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
                "content": "Rufe genau einmal das Tool list_files auf."
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
    print("=== SAFE AGENT V2 ===")
    print("Workspace:", getcwd())

    result = ask_model()
    content = result["message"]["content"]
    print("Model output:", content)

    tool_call = json.loads(content)
    name = tool_call["name"]

    if name == "list_files":
        print("Tool erkannt: list_files")
        print("Dateien:", list_files())
    elif name == "getcwd":
        print("Tool erkannt: getcwd")
        print("cwd:", getcwd())
    else:
        raise RuntimeError(f"Unerlaubtes Tool: {name}")

    print("SAFE AGENT V2 OK")


if __name__ == "__main__":
    main()
