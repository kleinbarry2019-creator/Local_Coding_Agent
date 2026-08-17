#!/usr/bin/env python3
import os, sys, json, hashlib, datetime, subprocess, re
import urllib.request, urllib.error
from pathlib import Path

# --- STATE MANAGEMENT ---
STATE_FILE = Path("agent_state.json")
STATE_BACKUP_FILE = Path("agent_state.json.bak")
STATE_HASH_FILE = Path("agent_state.sha256")
AUDIT_LOG_FILE = Path("audit_log.jsonl")

VERSION = 39
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5-coder:7b"

def get_default_state():
    return {"version": VERSION, "tasks_completed": 0, "last_result": "initialized"}

def validate_state(state):
    if not isinstance(state, dict):
        return get_default_state()
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
                if content:
                    return validate_state(json.loads(content))
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


# --- AUTONOMY ENGINE ---
def ask_llm(messages):
    payload = {"model": MODEL, "messages": messages, "stream": False}
    try:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))["message"]["content"]
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        return f'{{"action": "error", "message": "HTTP FEHLER {e.code}: {err_msg}"}}'
    except Exception as e:
        return f'{{"action": "error", "message": "Ollama Offline oder Fehler: {e}"}}'

def run_agent_loop(task_description, max_steps=15):
    state = load_state()
    
    system_prompt = """Du bist ein autonomer KI-Entwickler auf Bazzite Linux.
Antworte IMMER und AUSSCHLIESSLICH mit exakt EINEM JSON-Block. Schreibe KEINEN Text vor oder nach dem JSON!

Aktion 1: Befehl ausführen
{
  "action": "run_command",
  "command": "dein bash befehl"
}

Aktion 2: Fertig
{
  "action": "done",
  "result": "Lösung"
}"""
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"AUFGABE: {task_description}\nSende dein erstes JSON-Kommando:"}
    ]
    
    print(f"\n🚀 Starte autonome Mission:\n{task_description}\n")
    
    for step in range(max_steps):
        print(f"\n--- [SCHRITT {step+1}/{max_steps}] KI (Ollama '{MODEL}') denkt nach... ---")
        
        response = ask_llm(messages)
        print(f"🤖 [KI ANTWORTET]:\n{response.strip()}\n")
        messages.append({"role": "assistant", "content": response})
        
        match = re.search(r'\{.*?\}', response, re.DOTALL)
        if not match:
            print("⚠️ [FEHLER] Kein JSON in der Antwort gefunden.")
            messages.append({"role": "user", "content": 'FEHLER: Deine Antwort war kein JSON. Nur JSON senden!'})
            continue
            
        try:
            action_data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            print(f"⚠️ [FEHLER] JSON Parse Fehler: {e}")
            messages.append({"role": "user", "content": f"JSON Parse Fehler: {e}"})
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
            except Exception as e:
                output = f"EXECUTION ERROR: {e}"
                
            print(f"📊 [ERGEBNIS]:\n{output[:400]}" + ("...\n[Ausgabe gekürzt]" if len(output) > 400 else ""))
            messages.append({"role": "user", "content": f"Ergebnis:\n{output}\nWas ist der nächste Schritt?"})
            
        elif action == "error":
            print(f"❌ [ABBRUCH DURCH SYSTEM]: {action_data.get('message')}")
            break
            
        else:
            print(f"⚠️ [FEHLER] Unbekannte Aktion: {action}")
            messages.append({"role": "user", "content": f"Unbekannte Aktion '{action}'."})
            
    else:
        print("\n❌ [ABBRUCH]: Max Schritte erreicht.")
        state["last_result"] = "Abbruch: Max Schritte erreicht."
        save_state(state)

if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Zeige mit 'pwd' das aktuelle Verzeichnis an und schreibe 'Autonom' in agent_test.txt."
    run_agent_loop(task)
