import os
from pathlib import Path

os.chdir("/var/home/mklein/aider_workspace/autonomous_agent")
print("=== SAFE AGENT V43 MARKDOWN PROTOCOL ===")

target = "safe_agent_v43.py"

V43_CODE = r"""#!/usr/bin/env python3
import os, sys, json, hashlib, datetime, subprocess, re
import urllib.request, urllib.error
from pathlib import Path

# --- STATE MANAGEMENT ---
STATE_FILE = Path("agent_state.json")
STATE_BACKUP_FILE = Path("agent_state.json.bak")
STATE_HASH_FILE = Path("agent_state.sha256")
AUDIT_LOG_FILE = Path("audit_log.jsonl")

VERSION = 43
OLLAMA_URL = "[http://127.0.0.1:11434/api/chat](http://127.0.0.1:11434/api/chat)"
MODEL = "qwen2.5-coder:7b"

def get_default_state(): return {"version": VERSION, "tasks_completed": 0, "last_result": "initialized"}

def validate_state(state):
    if not isinstance(state, dict): return get_default_state()
    if "version" not in state: state["version"] = VERSION
    if "tasks_completed" not in state: state["tasks_completed"] = 0
    if "last_result" not in state: state["last_result"] = "initialized"
    return state

def calculate_state_hash(state): return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()

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
    if STATE_FILE.exists(): STATE_BACKUP_FILE.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
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
        return f'ACTION: error\nRESULT: HTTP FEHLER {e.code}'
    except Exception as e:
        return f'ACTION: error\nRESULT: Ollama Fehler: {e}'

def parse_response(text):
    action_match = re.search(r'ACTION:\s*(run_command|done|error)', text)
    if not action_match:
        return None, "FORMAT-FEHLER: 'ACTION: run_command' oder 'ACTION: done' nicht gefunden. Halte dich an das Format!'''
    
    action = action_match.group(1)
    
    if action == "run_command":
        cmd_match = re.search(r'COMMAND:\s*```(?:bash|sh|python)?\n?(.*?)```', text, re.DOTALL)
        if cmd_match: return "run_command", cmd_match.group(1).strip()
        
        cmd_match_fallback = re.search(r'COMMAND:\s*(.*)', text, re.DOTALL)
        if cmd_match_fallback: return "run_command", cmd_match_fallback.group(1).strip()
        
        return None, "FORMAT-FEHLER: 'COMMAND:' Block fehlt oder ist leer."
        
    elif action in ["done", "error"]:
        res_match = re.search(r'RESULT:\s*(.*)', text, re.DOTALL)
        res = res_match.group(1).strip() if res_match else "Kein Ergebnis angegeben."
        return action, res

def run_agent_loop(task_description, max_steps=15):
    state = load_state()
    
    system_prompt = '''Du bist ein autonomer KI-Entwickler auf Linux.
Vergiss JSON! Du antwortest IMMER exakt in diesem strikten Text-Format:

MÖGLICHKEIT 1: Shell-Befehl ausführen (Code per heredoc schreiben & testen)
ACTION: run_command
COMMAND:
```bash
cat << 'EOF' > script.py
import os
print("Hallo")

"""
