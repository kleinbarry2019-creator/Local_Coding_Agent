import os, re, glob, subprocess
from pathlib import Path

os.chdir("/var/home/mklein/aider_workspace/autonomous_agent")
print("=== SAFE AGENT PYTHON AUTOBUILD ===")

bases = sorted(glob.glob("safe_agent_v*.py"), key=lambda x: [int(c) for c in re.findall(r"\d+", x)])
if not bases:
    raise SystemExit("FEHLER: Keine Agent-Basis gefunden!")
base = bases[-1]
version = int(re.findall(r"\d+", base)[-1])
next_version = version + 1
target = f"safe_agent_v{next_version}.py"

print(f"Basis: {base} -> Ziel: {target}")
Path(target).write_text(Path(base).read_text(encoding="utf-8"), encoding="utf-8")

Path("tools").mkdir(exist_ok=True)
Path("docs").mkdir(exist_ok=True)
Path("backups").mkdir(exist_ok=True)

src_path = Path(target)
s = src_path.read_text(encoding="utf-8")
s = s.replace(f"SAFE AGENT V{version}", f"SAFE AGENT V{next_version}")

if "STATE_BACKUP_FILE" not in s:
    s = s.replace(
        "STATE_FILE = Path(\"agent_state.json\")",
        "STATE_FILE = Path(\"agent_state.json\")\nSTATE_BACKUP_FILE = Path(\"agent_state.json.bak\")\nSTATE_HASH_FILE = Path(\"agent_state.sha256\")\nAUDIT_LOG_FILE = Path(\"audit_log.jsonl\")"
    )

if "def validate_state" not in s:
    validator = """
def validate_state(state):
    if not isinstance(state, dict):
        raise RuntimeError("FAIL-CLOSED: state ist kein dict")
    required = {"version", "tasks_completed", "last_result"}
    missing = required - set(state)
    if missing:
        raise RuntimeError(f"FAIL-CLOSED: fehlende Felder: {missing}")
    if not isinstance(state["version"], int):
        raise RuntimeError("FAIL-CLOSED: 'version' muss int sein")
    if not isinstance(state["tasks_completed"], int):
        raise RuntimeError("FAIL-CLOSED: 'tasks_completed' muss int sein")
    return state
"""
    marker = "def load_state():"
    s = s.replace(marker, validator.strip() + "\n\n\n" + marker, 1)

new_helpers = ""
if "def calculate_state_hash" not in s:
    new_helpers += """
def calculate_state_hash(state):
    import hashlib, json as _json
    return hashlib.sha256(_json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()
"""
if "def audit_log" not in s:
    new_helpers += """
def audit_log(event, details=None):
    import json as _json, datetime as _dt
    entry = {"timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(), "event": event, "details": details or {}}
    with AUDIT_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry) + "\\n")
"""

if new_helpers:
    main_guard = re.search(r"^if __name__ == [\"\x27]__main__[\"\x27]:", s, re.MULTILINE)
    if main_guard:
        idx = main_guard.start()
        s = s[:idx] + new_helpers.strip() + "\n\n\n" + s[idx:]
    else:
        s = s.rstrip() + "\n" + new_helpers

save_state_pattern = re.compile(r"def save_state\(state\):\n(?:[ \t].*\n)*", re.MULTILINE)
new_save_state = """def save_state(state):
    validate_state(state)
    if STATE_FILE.exists():
        STATE_BACKUP_FILE.write_text(STATE_FILE.read_text(), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    STATE_HASH_FILE.write_text(calculate_state_hash(state), encoding="utf-8")
    audit_log("state_saved", {"tasks_completed": state.get("tasks_completed")})
"""
s = save_state_pattern.sub(new_save_state, s, count=1)
src_path.write_text(s, encoding="utf-8")

Path("tools/test_all.sh").write_text("""#!/usr/bin/env bash
set -Eeuo pipefail
python3 -m py_compile safe_agent.py
python3 safe_agent.py
python3 safe_agent.py
for f in agent_state.json agent_state.json.bak agent_state.sha256; do
    [ -f "$f" ] || exit 1
done
echo "Alle Tests bestanden."
""", encoding="utf-8")
subprocess.run(["chmod", "+x", "tools/test_all.sh"])

Path("tools/verify_state_hash.sh").write_text("""#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
python3 -c "
import hashlib, json
from pathlib import Path
state = json.loads(Path('agent_state.json').read_text())
actual = hashlib.sha256(json.dumps(state, sort_keys=True).encode('utf-8')).hexdigest()
expected = Path('agent_state.sha256').read_text().strip()
assert actual == expected, 'MISMATCH'
print('OK: Hash stimmt')
"
""", encoding="utf-8")
subprocess.run(["chmod", "+x", "tools/verify_state_hash.sh"])

if Path("safe_agent.py").is_symlink() or Path("safe_agent.py").exists():
    Path("safe_agent.py").unlink()
Path("safe_agent.py").symlink_to(target)

print(f"=== SAFE AGENT V{next_version} READY ===")
