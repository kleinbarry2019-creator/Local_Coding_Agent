import json
import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path


MODEL = "qwen2.5-coder:7b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

WORKSPACE = Path.cwd().resolve()

MAX_MODEL_STEPS = 5
MAX_TOOL_CALLS = 1

MAX_OUTPUT_CHARS = 4096
COMMAND_TIMEOUT = 10

ALLOWED_TOOLS = {"run_python_sandbox"}
ALLOWED_SCRIPTS = {"safe_test.py"}


MAX_STATE_BYTES = 16384

AUDIT_FILE = Path("audit_log.jsonl")
MAX_AUDIT_BYTES = 1048576




def audit(event, data=None):

    import json
    import time

    entry = {
        "time": time.time(),
        "event": event,
        "data": data,
    }

    if AUDIT_FILE.exists():

        if AUDIT_FILE.stat().st_size > MAX_AUDIT_BYTES:
            raise RuntimeError(
                "FAIL-CLOSED: Audit Log zu groß"
            )

    with AUDIT_FILE.open(
        "a",
        encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                entry,
                ensure_ascii=False
            )
            + "\n"
        )




def verify_state_hash(state):

    if not STATE_HASH_FILE.exists():
        return False

    stored = STATE_HASH_FILE.read_text(
        encoding="utf-8"
    ).strip()

    return stored == calculate_state_hash(state)



def write_state_hash(state):

    digest = calculate_state_hash(state)

    STATE_HASH_FILE.write_text(
        digest,
        encoding="utf-8"
    )





def resolve_safe_script(script: str) -> Path:
    if not isinstance(script, str):
        raise TypeError("script muss ein String sein")

    if script not in ALLOWED_SCRIPTS:
        raise PermissionError(
            f"Skript nicht freigegeben: {script}"
        )

    target = (WORKSPACE / script).resolve(strict=False)

    try:
        target.relative_to(WORKSPACE)
    except ValueError as exc:
        raise PermissionError(
            f"Skript außerhalb der Sandbox: {script}"
        ) from exc

    if target.suffix != ".py":
        raise PermissionError("Nur Python-Skripte sind erlaubt.")

    if not target.is_file():
        raise FileNotFoundError(
            f"Skript nicht gefunden: {script}"
        )

    return target


def run_python_sandbox(script):
    target = resolve_safe_script(script)

    command = [
        "/usr/bin/bwrap",
        "--die-with-parent",

        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup",
        "--cap-drop", "ALL",
        "--die-with-parent",

        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",

        "--proc", "/proc",
        "--dev", "/dev",

        "--size", "536870912",
        "--tmpfs", "/tmp",

        "--ro-bind", str(WORKSPACE), "/workspace",
        "--chdir", "/workspace",

        sys.executable,
        "/workspace/" + target.name,
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=WORKSPACE,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT,
            shell=False,
            check=False,
        )

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "TIMEOUT",
            "exit_code": None,
            "stdout": "",
            "stderr": "Sandbox timeout",
            "script": script,
        }


    stdout = completed.stdout[:MAX_OUTPUT_CHARS]
    stderr = completed.stderr[:MAX_OUTPUT_CHARS]

    return {
        "ok": completed.returncode == 0,
        "status": "COMPLETED",
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "script": script,
    }


def policy_check(name, arguments):
    if name not in ALLOWED_TOOLS:
        return False, f"Tool nicht erlaubt: {name}"

    if not isinstance(arguments, dict):
        return False, "Argumente müssen ein Objekt sein."

    if set(arguments) != {"script"}:
        return False, "Nur das Argument 'script' ist erlaubt."

    try:
        resolve_safe_script(arguments["script"])
    except (PermissionError, TypeError, FileNotFoundError) as exc:
        return False, str(exc)

    return True, "ALLOW"


def execute_tool(name, arguments):
    allowed, reason = policy_check(name, arguments)

    print(f"POLICY: {name} -> {reason}")

    if not allowed:
        raise PermissionError(reason)

    return run_python_sandbox(arguments["script"])


def call_model(messages):
    system_prompt = """Du bist ein strikt kontrollierter Test-Agent.

Antworte immer mit genau EINEM JSON-Objekt.

Tool:
{"name":"run_python_sandbox","arguments":{"script":"safe_test.py"}}

Final:
{"final":"TEXT"}

Regeln:
- Kein Markdown.
- Kein Python.
- Kein Shell-Code.
- Keine Befehle.
- Keine mehreren JSON-Objekte.
- Nur das bereitgestellte Tool.
- Nur safe_test.py darf angefordert werden.
- Höchstens ein Tool pro Antwort.
"""

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            *messages,
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "run_python_sandbox",
                    "description": (
                        "Führt safe_test.py in einer isolierten "
                        "bubblewrap-Sandbox aus."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "script": {
                                "type": "string"
                            }
                        },
                        "required": ["script"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
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


def parse_response(content):
    text = content.strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "FAIL-CLOSED: ungültiges JSON."
        ) from exc

    if not isinstance(obj, dict):
        raise RuntimeError(
            "FAIL-CLOSED: JSON-Objekt erwartet."
        )

    if "name" in obj or "arguments" in obj:
        if set(obj) != {"name", "arguments"}:
            raise RuntimeError(
                "FAIL-CLOSED: ungültiges Tool-Schema."
            )

        if not isinstance(obj["name"], str):
            raise RuntimeError(
                "FAIL-CLOSED: Toolname ungültig."
            )

        if not isinstance(obj["arguments"], dict):
            raise RuntimeError(
                "FAIL-CLOSED: arguments ungültig."
            )

        return {
            "type": "tool",
            "name": obj["name"],
            "arguments": obj["arguments"],
        }

    if "final" in obj:
        if set(obj) != {"final"}:
            raise RuntimeError(
                "FAIL-CLOSED: ungültiges Final-Schema."
            )

        if not isinstance(obj["final"], str):
            raise RuntimeError(
                "FAIL-CLOSED: final ungültig."
            )

        return {
            "type": "final",
            "content": obj["final"],
        }

    raise RuntimeError(
        "FAIL-CLOSED: unbekanntes Antwortschema."
    )



from pathlib import Path
import json, hashlib, datetime

STATE_FILE = Path("agent_state.json")
STATE_BACKUP_FILE = Path("agent_state.json.bak")
STATE_HASH_FILE = Path("agent_state.sha256")
AUDIT_LOG_FILE = Path("audit_log.jsonl")

def get_default_state():
    return {"version": 36, "tasks_completed": 0, "last_result": "initialized"}

def validate_state(state):
    if not isinstance(state, dict):
        return get_default_state()
    if "version" not in state:
        state["version"] = 36
    if "tasks_completed" not in state:
        state["tasks_completed"] = 0
    if "last_result" not in state:
        state["last_result"] = "initialized"
    return state

def calculate_state_hash(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()

def audit_log(event, details=None):
    entry = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "event": event, "details": details or {}}
    with AUDIT_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

def load_state():
    for fpath in [STATE_FILE, STATE_BACKUP_FILE]:
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8").strip()
                if content:
                    data = json.loads(content)
                    return validate_state(data)
            except Exception:
                continue
    default_state = get_default_state()
    save_state(default_state)
    return default_state

def save_state(state):
    state = validate_state(state)
    if STATE_FILE.exists():
        STATE_BACKUP_FILE.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    STATE_HASH_FILE.write_text(calculate_state_hash(state), encoding="utf-8")
    audit_log("state_saved", {"tasks_completed": state.get("tasks_completed")})


import urllib.request, urllib.error, subprocess

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5-coder-7b"

def ask_llm(messages):
    payload = {"model": MODEL, "messages": messages, "stream": False}
    try:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))["message"]["content"]
    except Exception as e:
        return f'{"{"}"action": "error", "message": "Ollama Verbindungsfehler: {e}"{"}"}'

def run_agent_loop(task_description, max_steps=15):
    state = load_state()
    
    system_prompt = """Du bist ein komplett autonomer KI-Entwickler auf einem Linux-System (Bazzite).
Du löst Aufgaben systematisch in einer Schleife.
Antworte IMMER und AUSSCHLIESSLICH mit exakt EINEM JSON-Block. Schreibe absolut KEINEN Text außerhalb des JSON-Blocks!

Aktion 1: Einen Shell-Befehl ausführen (z.B. Dateien lesen, schreiben, Code ausführen)
{
  "action": "run_command",
  "command": "dein bash befehl hier"
}

Aktion 2: Die Aufgabe ist komplett abgeschlossen
{
  "action": "done",
  "result": "Zusammenfassung der erreichten Lösung"
}"""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"AUFGABE: {task_description}\nAnalysiere die Aufgabe und sende mir dein erstes JSON-Aktions-Kommando."}
    ]
    
    print(f"\n🚀 Starte autonome Mission:\n{task_description}\n")
    
    for step in range(max_steps):
        print(f"\n--- [SCHRITT {step+1}/{max_steps}] KI denkt nach (Ollama {MODEL})... ---")
        
        response = ask_llm(messages)
        messages.append({"role": "assistant", "content": response})
        
        import re
        match = re.search(r'\{.*?\}', response, re.DOTALL)
        if not match:
            print(f"⚠️ [WARNUNG] Kein JSON gefunden:\n{response}")
            messages.append({"role": "user", "content": 'FEHLER: Deine Antwort enthielt kein gültiges JSON. Bitte überdenke das und antworte NUR mit JSON, z.B. {"action": "run_command", "command": "..."}'})
            continue
            
        try:
            action_data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            messages.append({"role": "user", "content": f"FEHLER beim JSON parsen: {e}. Achte auf korrekte Anführungszeichen und Escaping."})
            continue
            
        action = action_data.get("action")
        
        if action == "done":
            res_text = action_data.get('result', 'Fertig.')
            print(f"\n✅ [MISSION ERFOLGREICH]: {res_text}\n")
            state["tasks_completed"] += 1
            state["last_result"] = str(res_text)
            save_state(state)
            break
            
        elif action == "run_command":
            cmd = action_data.get("command", "")
            print(f"⚙️  [FÜHRE AUS]: {cmd}")
            try:
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                output = f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nEXIT_CODE: {res.returncode}"
            except subprocess.TimeoutExpired:
                output = "FEHLER: Timeout. Der Befehl hing fest und wurde abgebrochen."
            except Exception as e:
                output = f"EXECUTION ERROR: {e}"
                
            print(f"📊 [ERGEBNIS]:\n{output[:400]}" + ("...\n[Ausgabe gekürzt]" if len(output) > 400 else ""))
            messages.append({"role": "user", "content": f"Ergebnis des Befehls:\n{output}\nWas ist der nächste Schritt?"})
            
        else:
            messages.append({"role": "user", "content": f"FEHLER: Unbekannte Aktion '{action}'."})
            
    else:
        print("\n❌ [ABBRUCH]: Maximale Anzahl an Schritten erreicht.")
        state["last_result"] = "Abbruch: Max Schritte erreicht."
        save_state(state)

if __name__ == "__main__":
    import sys
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Zeige mit 'pwd' das aktuelle Verzeichnis an, liste alle Dateien auf und erstelle dann eine Datei 'agent_test.txt' mit dem Text 'Der Agent ist jetzt wirklich autonom.'."
    run_agent_loop(task)
