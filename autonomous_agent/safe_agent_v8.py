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
    # 1. Tool überhaupt erlaubt?
    if name not in ALLOWED_TOOLS:
        return False, f"Tool nicht freigegeben: {name}"

    # 2. Argumente müssen ein Objekt sein.
    if not isinstance(arguments, dict):
        return False, "Argumente sind kein Objekt."

    # 3. Spezielle Prüfung für write_file.
    if name == "write_file":
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


def call_model(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
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


def main():
    print("=== SAFE AGENT V8 ===")
    print("Workspace:", WORKSPACE)

    messages = [
        {
            "role": "user",
            "content": (
                "Schreibe exakt einmal mit write_file die Datei "
                "agent_v8_test.txt mit dem Inhalt "
                "\"V8 POLICY OK\"."
            ),
        }
    ]

    result = call_model(messages)
    content = result["message"].get("content", "").strip()

    print("Model:")
    print(content)

    calls = parse_tool_calls(content)

    if not calls:
        raise RuntimeError("Kein Tool-Aufruf erkannt.")

    executed = set()

    for call in calls:
        name = call.get("name")
        arguments = call.get("arguments", {})

        call_key = json.dumps(
            {
                "name": name,
                "arguments": arguments,
            },
            sort_keys=True,
        )

        # Idempotenz: exakt derselbe Aufruf nur einmal.
        if call_key in executed:
            print("BLOCK: doppelter Tool-Aufruf")
            continue

        executed.add(call_key)

        print()
        print("Tool:", name)
        print("Arguments:", arguments)

        result = execute_tool(name, arguments)

        print("Result:", result)

    target = WORKSPACE / "agent_v8_test.txt"

    if not target.exists():
        raise RuntimeError("V8-Testdatei wurde nicht erzeugt.")

    if target.read_text(encoding="utf-8") != "V8 POLICY OK":
        raise RuntimeError("V8-Testdatei enthält falschen Inhalt.")

    print()
    print("SAFE AGENT V8 OK")


if __name__ == "__main__":
    main()
