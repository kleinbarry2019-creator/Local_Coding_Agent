import os, re, glob, subprocess
from pathlib import Path

os.chdir("/var/home/mklein/aider_workspace/autonomous_agent")
print("=== SAFE AGENT V37 AUTONOMY UPGRADE ===")

bases = sorted(glob.glob("safe_agent_v*.py"), key=lambda x: [int(c) for c in re.findall(r"\d+", x)])
base = bases[-1]
version = int(re.findall(r"\d+", base)[-1])
next_version = version + 1
target = f"safe_agent_v{next_version}.py"

print(f"Basis: {base} -> Ziel: {target}")
s = Path(base).read_text(encoding="utf-8")
s = s.replace(f"SAFE AGENT V{version}", f"SAFE AGENT V{next_version}")

# Den alten Dummy-Code entfernen
if "def main():" in s:
    s = s.split("def main():")[0]

# Das echte Ollama-Gehirn und den Autonomie-Loop einsetzen
AUTONOMY_LOGIC = r'''
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
'''

Path(target).write_text(s.strip() + "\n\n" + AUTONOMY_LOGIC, encoding="utf-8")

# Test-Script aktualisieren, damit es nicht mehr die blockierende Main aufruft, sondern nur Syntax & Hash prüft
Path("tools/test_all.sh").write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\npython3 -m py_compile safe_agent.py\n./tools/verify_state_hash.sh\necho 'Syntax & State OK.'\n", encoding="utf-8")
subprocess.run(["chmod", "+x", "tools/test_all.sh"])

if Path("safe_agent.py").is_symlink() or Path("safe_agent.py").exists():
    Path("safe_agent.py").unlink()
Path("safe_agent.py").symlink_to(target)

print(f"=== SAFE AGENT V{next_version} READY ===")
