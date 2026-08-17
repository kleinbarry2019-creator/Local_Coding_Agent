import json
import urllib.request

payload = {
    "model": "qwen2.5-coder:7b-instruct",
    "messages": [
        {
            "role": "user",
            "content": "Rufe das Tool getcwd genau einmal auf."
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

req = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)

with urllib.request.urlopen(req, timeout=60) as response:
    result = json.load(response)

print(json.dumps(result, indent=2))
