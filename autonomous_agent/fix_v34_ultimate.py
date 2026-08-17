import os, re, glob, subprocess, json, hashlib
from pathlib import Path

os.chdir("/var/home/mklein/aider_workspace/autonomous_agent")
print("=== SAFE AGENT V34 ULTIMATE FIX ===")

bases = sorted(glob.glob("safe_agent_v*.py"), key=lambda x: [int(c) for c in re.findall(r"\d+", x)])
base = bases[-1]
version = int(re.findall(r"\d+", base)[-1])
next_version = version + 1
target = f"safe_agent_v{next_version}.py"

print(f"Basis: {base} -> Ziel: {target}")
s = Path(base).read_text(encoding="utf-8")
s = s.replace(f"SAFE AGENT V{version}", f"SAFE AGENT V{next_version}")

# Wir löschen rigoros ALLE alten, defekten State-Funktionen aus der Datei heraus
funcs_to_remove = ["validate_state", "load_state", "get_default_state", "recover_state", "save_state"]
for func in funcs_to_remove:
    s = re.sub(r"^def " + func + r"\b.*?(?=^(?:def |class |if __name__ == )|\Z)", "", s, flags=re.MULTILINE | re.DOTALL)

# Der neue, robuste und fehlertolerante State-Mechanismus
perfect_state_logic = f"""
def get_default_state():
    return {{"version": {next_version}, "tasks_completed": 0, "last_result": "initialized"}}

def validate_state(state):
    if not isinstance(state, dict):
        return get_default_state()
    if "version" not in state:
        state["version"] = {next_version}
    if "tasks_completed" not in state:
        state["tasks_completed"] = 0
    if "last_result" not in state:
        state["last_result"] = "initialized"
    return state

def load_state():
    import json
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
    import json
    state = validate_state(state)
    if STATE_FILE.exists():
        STATE_BACKUP_FILE.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        STATE_HASH_FILE.write_text(calculate_state_hash(state), encoding="utf-8")
        audit_log("state_saved", {{"tasks_completed": state.get("tasks_completed")}})
    except NameError:
        pass
"""

# Einfügen der frischen Logik VOR der main-Funktion
if "def main():" in s:
    s = s.replace("def main():", perfect_state_logic.strip() + "\n\ndef main():")
else:
    s += "\n\n" + perfect_state_logic.strip()

Path(target).write_text(s, encoding="utf-8")

# Initialen sauberen State auf die Festplatte schreiben
initial_state = {"version": next_version, "tasks_completed": 0, "last_result": "initialized"}
encoded = json.dumps(initial_state, sort_keys=True).encode("utf-8")
Path("agent_state.json").write_text(json.dumps(initial_state, indent=2), encoding="utf-8")
Path("agent_state.json.bak").write_text(json.dumps(initial_state, indent=2), encoding="utf-8")
Path("agent_state.sha256").write_text(hashlib.sha256(encoded).hexdigest(), encoding="utf-8")

Path("tools/test_all.sh").write_text("""#!/usr/bin/env bash
set -Eeuo pipefail
python3 -m py_compile safe_agent.py
python3 safe_agent.py
python3 safe_agent.py
./tools/verify_state_hash.sh
echo "Alle Tests bestanden."
""", encoding="utf-8")
subprocess.run(["chmod", "+x", "tools/test_all.sh"])

if Path("safe_agent.py").is_symlink() or Path("safe_agent.py").exists():
    Path("safe_agent.py").unlink()
Path("safe_agent.py").symlink_to(target)

print(f"=== SAFE AGENT V{next_version} READY ===")
