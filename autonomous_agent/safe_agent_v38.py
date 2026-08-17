import json
import hashlib
import subprocess
import sys


import urllib.request, urllib.error, subprocess

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
# Standard Ollama Notation mit Doppelpunkt!
MODEL = "qwen2.5-coder:7b" 

def ask_llm(messages):
    payload = {"model": MODEL, "messages": messages, "stream": False}
    try:
        req = urllib.request.Request(OLLAMA_URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))["message"]["content"]
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        return f'{"{"}"action": "error", "message": "HTTP FEHLER {e.code}: {err_msg}"{"}"}'
    except Exception as e:
        return f'{"{"}"action": "error", "message": "Ollama Offline oder Fehler: {e}"{"}"}'

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
        
        import re
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
    import sys
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Zeige mit 'pwd' das aktuelle Verzeichnis an und schreibe 'Autonom' in agent_test.txt."
    run_agent_loop(task)
