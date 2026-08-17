import json
import urllib.request


payload = {
    "model": "qwen2.5-coder:7b-instruct",
    "messages": [
        {
            "role": "user",
            "content": "Rufe genau einmal das Tool getcwd auf."
        }
    ],
    "tools": [
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
        }
    ],
    "options": {
        "num_ctx": 1024,
        "temperature": 0
    },
    "stream": False
}

request = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)

with urllib.request.urlopen(request, timeout=60) as response:
    result = json.load(response)

message = result.get("message", {})

print("=== MESSAGE ===")
print(json.dumps(message, indent=2, ensure_ascii=False))

print()
print("=== TOOL_CALLS ===")
print(json.dumps(
    message.get("tool_calls"),
    indent=2,
    ensure_ascii=False
))

print()
print("=== CONTENT ===")
print(repr(message.get("content")))
