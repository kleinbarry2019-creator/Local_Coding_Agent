import os, re, glob, subprocess, json, hashlib
from pathlib import Path

os.chdir("/var/home/mklein/aider_workspace/autonomous_agent")
print("=== SAFE AGENT V34 HARD FIX ===")

bases = sorted(glob.glob("safe_agent_v*.py"), key=lambda x: [int(c) for c in re.findall(r"\d+", x)])
base = bases[-1]
version = int(re.findall(r"\d+", base)[-1])
next_version = version + 1
target = f"safe_agent_v{next_version}.py"

lines = Path(base).read_text(encoding="utf-8").splitlines()

# Wir filtern die kaputten Funktionen rigoros heraus
clean_lines = []
skip = False
for line in lines:
    if line.startswith("def load_state()"):
        skip = True
    elif line.startswith("def get_default_state()"):
        skip = True
    elif skip and line and not line.startswith(" ") and not line.startswith("\t"):
        skip = False
        
    if not skip:
        clean_lines.append(line.replace(f"SAFE AGENT V{version}", f"SAFE AGENT V{next_version}"))

s = "\n".join(clean_lines)

perfect_load_state = f"""
def get_default_state():
    return {{"version": {next_version}, "tasks_completed": 0, "last_result": "initialized"}}

def load_state():
    for fpath in [STATE_FILE, STATE_BACKUP_FILE]:
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8").strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        return validate_state(data)
            except Exception:
                continue
    default_state = get_default_state()
    save_state(default_state)
    return default_state
"""

# Einfügen VOR der main-Funktion
if "def main():" in s:
    s = s.replace("def main():", perfect_load_state.strip() + "\n\ndef main():")
else:
    s += "\n" + perfect_load_state.strip()

Path(target).write_text(s, encoding="utf-8")

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
