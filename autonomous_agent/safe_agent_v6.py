import json
import os
import urllib.request


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

ALLOWED_TOOLS = {"getcwd", "list_files"}


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


def getcwd():
    return os.getcwd()


def list_files():
    return sorted(os.listdir("."))


def call_model(messages, tools=None):
    payload = {
        "model": MODEL,
        "messages": messages,
        "options": {
            "num_ctx": 1024,
            "temperature": 0
        },
        "stream": False
    }

    if tools is not None:
        payload["tools"] = tools

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
    print("=== SAFE AGENT V6 ===")

    state = {
        "getcwd": None,
        "list_files": None,
    }

    planning_messages = [
        {
            "role": "user",
            "content": (
                "Du sollst zwei Informationen ermitteln: "
                "1. das aktuelle Arbeitsverzeichnis, "
                "2. die Dateien im aktuellen Arbeitsverzeichnis. "
                "Rufe dafür die benötigten Tools auf. "
                "Jedes benötigte Tool höchstens einmal."
            )
        }
    ]

    for step in range(3):
        print(f"\n--- Planungsschritt {step + 1} ---")

        result = call_model(planning_messages, TOOLS)
        content = result["message"].get("content", "").strip()

        print("Model:")
        print(content)

        tool_calls = parse_tool_calls(content)

        if not tool_calls:
            break

        new_call_executed = False

        for tool_call in tool_calls:
            name = tool_call.get("name")
            arguments = tool_call.get("arguments", {})

            if name not in ALLOWED_TOOLS:
                print(f"BLOCKIERT: {name}")
                continue

            if state[name] is not None:
                print(f"ÜBERSPRUNGEN: {name} wurde bereits ausgeführt.")
                continue

            print(f"Tool: {name}")
            print(f"Arguments: {arguments}")

            result_data = execute_tool(name, arguments)
            state[name] = result_data
            new_call_executed = True

            print("Ergebnis:", result_data)

        if not new_call_executed:
            break

        planning_messages.append(
            {
                "role": "assistant",
                "content": content,
            }
        )

        planning_messages.append(
            {
                "role": "user",
                "content": (
                    "Bereits ermittelte Daten:\n"
                    + json.dumps(state, ensure_ascii=False)
                    + "\n"
                    "Fordere keine bereits ausgeführten Tools erneut an. "
                    "Wenn alle benötigten Informationen vorhanden sind, "
                    "beende die Tool-Phase."
                ),
            }
        )

        if state["getcwd"] is not None and state["list_files"] is not None:
            break

    print("\n=== TOOL-PHASE BEENDET ===")
    print(json.dumps(state, indent=2, ensure_ascii=False))

    final_messages = [
        {
            "role": "system",
            "content": (
                "Du formulierst jetzt nur die finale Antwort. "
                "Keine Tool-Aufrufe. Keine JSON-Tool-Calls."
            ),
        },
        {
            "role": "user",
            "content": (
                "Fasse die folgenden Ergebnisse kurz zusammen:\n"
                + json.dumps(state, indent=2, ensure_ascii=False)
            ),
        },
    ]

    final_result = call_model(final_messages, tools=None)
    final_text = final_result["message"].get("content", "").strip()

    print("\n=== FINALE ANTWORT ===")
    print(final_text)
    print("\nSAFE AGENT V6 OK")


if __name__ == "__main__":
    main()
