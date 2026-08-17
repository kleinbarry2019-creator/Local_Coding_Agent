import json
import os
import urllib.request


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

ALLOWED_TOOLS = {"getcwd", "list_files"}


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
            "description": "Listet Dateien im aktuellen Arbeitsverzeichnis auf.",
            "parameters": {
                "type": "object",
                "properties": {}
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
        return {"cwd": getcwd()}

    if name == "list_files":
        return {"files": list_files()}

    raise RuntimeError(f"Unbekanntes Tool: {name}")


def main():
    messages = [
        {
            "role": "user",
            "content": (
                "Finde zuerst das aktuelle Arbeitsverzeichnis. "
                "Danach liste die Dateien auf. "
                "Verwende dafür die verfügbaren Tools."
            )
        }
    ]

    print("=== SAFE AGENT V5 ===")

    for step in range(3):
        print(f"\n--- Schritt {step + 1} ---")

        result = call_model(messages)
        message = result["message"]
        content = message.get("content", "").strip()

        print("Model:")
        print(content)

        tool_calls = parse_tool_calls(content)

        if not tool_calls:
            print("\nFINAL:", content)
            return

        messages.append(
            {
                "role": "assistant",
                "content": content
            }
        )

        for index, tool_call in enumerate(tool_calls, start=1):
            name = tool_call.get("name")
            arguments = tool_call.get("arguments", {})

            print(f"\nTool {index}: {name}")
            print("Arguments:", arguments)

            tool_result = execute_tool(name, arguments)

            print("Ergebnis:", tool_result)

            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Tool-Ergebnis für {name}:\n"
                        + json.dumps(tool_result, ensure_ascii=False)
                    )
                }
            )

    print("\nABBRUCH: maximales Schrittlimit erreicht.")


if __name__ == "__main__":
    main()
