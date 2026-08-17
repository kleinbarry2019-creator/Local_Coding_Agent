import os
from pathlib import Path

os.chdir("/var/home/mklein/aider_workspace/autonomous_agent")
print("=== SAFE AGENT V41 ROBUST JSON FIX ===")

target = "safe_agent_v41.py"

V41_CODE = r'''#!/usr/bin/env python3
import os, sys, json, hashlib, datetime, subprocess, re
import urllib.request, urllib.error
from pathlib import Path

# --- STATE MANAGEMENT ---
STATE_FILE = Path("agent_state.json")
STATE_BACKUP_FILE = Path("agent_state.json.bak")
STATE_HASH_FILE = Path("agent_state.sha256")
AUDIT_LOG_FILE = Path("audit_log.jsonl")

VERSION = 41
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5-coder:7b"

def get_default_state():
    return {"version": VERSION, "tasks_completed": 0, "last_result": "initialized"}

def validate_state(state):
    if not isinstance(state, dict): return get_default_state()
    if "version" not in state: state["version"] = VERSION
    if "tasks_completed" not in state: state["tasks_completed"] = 0
    if "last_result" not in state: state["last_result"] = "initialized"
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
                if content: return validate_state(json.loads(content))
            except Exception: continue
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

# --- AUTONOMY ENGINE ---
def ask_llm(messages):
    payload = {"model": MODEL, "messages": messages, "stream": False}
    try:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))["message"]["content"]
    except urllib.error.HTTPError as e:
        return f'{{"action": "error", "message": "HTTP FEHLER {e.code}"}}'
    except Exception as e:
        return f'{{"action": "error", "message": "Ollama Fehler: {e}"}}'

def run_agent_loop(task_description, max_steps=15):
    state = load_state()
    
    system_prompt = """Du bist ein autonomer KI-Entwickler auf Linux.
Antworte IMMER mit exakt EINEM JSON-Block. Verwende KEINEN Markdown-Text außerhalb des JSON-Blocks!

Aktion 1: Shell-Befehl ausführen
{
  "action": "run_command",
  "command": "python3 script.py"
}

Aktion 2: Datei erstellen/überschreiben (NUTZE DIES UM CODE ZU SCHREIBEN, statt Bash-Echo/Cat!)
{
  "action": "write_file",
  "file": "sysinfo.py",
  "content": "import os\nprint('Hallo')"
}

Aktion 3: Aufgabe komplett abgeschlossen
{
  "action": "done",
  "result": "Erklärung, was gemacht wurde"
}"""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"AUFGABE: {task_description}\nSende dein erstes JSON-Kommando:"}
    ]
    
    print(f"\n🚀 Mission:\n{task_description}\n")
    
    for step in range(max_steps):
        print(f"\n--- [SCHRITT {step+1}/{max_steps}] KI denkt nach... ---")
        response = ask_llm(messages)
        print(f"🤖 [KI ANTWORTET]:\n{response.strip()}\n")
        messages.append({"role": "assistant", "content": response})
        
        match = re.search(r'\{.*?\}', response, re.DOTALL)
        if not match:
            messages.append({"role": "user", "content": 'FEHLER: Kein JSON gefunden! Sende NUR JSON.'})
            continue
            
        json_str = match.group(0).replace("\\'", "'")
        
        try:
            # strict=False rettet uns bei un-escapten echten Zeilenumbrüchen!
            action_data = json.loads(json_str, strict=False)
        except json.JSONDecodeError as e:
            messages.append({"role": "user", "content": f"JSON Fehler: {e}. Nutze bei Code-Dateien ZWINGEND die Aktion 'write_file'!"})
            continue
            
        action = action_data.get("action")
        
        if action == "done":
            res = action_data.get('result', 'Fertig.')
            print(f"\n✅ [ERFOLG]: {res}\n")
            state["tasks_completed"] += 1
            state["last_result"] = str(res)
            save_state(state)
            break
            
        elif action == "run_command":
            cmd = action_data.get("command", "")
            print(f"⚙️  [BASH]: {cmd}")
            try:
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
                out = f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nEXIT: {res.returncode}"
            except Exception as e:
                out = f"ERROR: {e}"
            print(f"📊 [ERGEBNIS]:\n{out[:400]}" + ("...\n" if len(out) > 400 else ""))
            messages.append({"role": "user", "content": f"Befehl ausgeführt. Ergebnis:\n{out}\nNächster Schritt?"})
            
        elif action == "write_file":
            filepath = action_data.get("file", "")
            content = action_data.get("content", "")
            print(f"📝 [DATEI SCHREIBEN]: {filepath}")
            try:
                Path(filepath).write_text(content, encoding="utf-8")
                out = f"Datei '{filepath}' erfolgreich gespeichert."
            except Exception as e:
                out = f"Schreibfehler: {e}"
            print(f"📊 [ERGEBNIS]: {out}")
            messages.append({"role": "user", "content": f"Ergebnis:\n{out}\nNächster Schritt?"})
            
        elif action == "error":
            print(f"❌ [ABBRUCH DURCH KI/SYSTEM]: {action_data.get('message')}")
            break
        else:
            messages.append({"role": "user", "content": f"Unbekannte Aktion '{action}'."})
    else:
        print("\n❌ [ABBRUCH]: Maximale Schritte erreicht.")

if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "pwd"
    run_agent_loop(task)
'''

Path(target).write_text(V41_CODE, encoding="utf-8")

if Path("safe_agent.py").is_symlink() or Path("safe_agent.py").exists():
    Path("safe_agent.py").unlink()
Path("safe_agent.py").symlink_to(target)

print("=== SAFE AGENT V41 READY ===")
