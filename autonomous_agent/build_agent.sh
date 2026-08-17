#!/usr/bin/env bash
set -Eeuo pipefail

echo "=================================="
echo " SAFE AGENT AUTOBUILD SYSTEM"
echo "=================================="

ROOT="$(pwd)"
echo -e "\n[1/12] Workspace\n$ROOT"

echo -e "\n[2/12] Basis-Agent suchen"
BASE=$(ls safe_agent_v*.py 2>/dev/null | sort -V | tail -1 || true)
if [ -z "$BASE" ]; then
    echo "FEHLER: Keine Agent-Basis gefunden (erwartet: safe_agent_v<N>.py)"
    exit 1
fi

VERSION=$(echo "$BASE" | grep -o '[0-9]\+' | tail -1)
NEXT=$((VERSION + 1))
TARGET="safe_agent_v${NEXT}.py"
echo "Basis: $BASE"
echo "Ziel:  $TARGET"

echo -e "\n[3/12] Version erzeugen"
cp "$BASE" "$TARGET"
echo "Agent V${NEXT} vorbereitet"

echo -e "\n[4/12] Verzeichnislayout"
mkdir -p tools docs backups

echo -e "\n[5/12] & [6/12] State-System & Audit-System vorbereiten"
python3 -c '
import os, re
from pathlib import Path

target = "'"$TARGET"'"
version = "'"$VERSION"'"
next_version = "'"$NEXT"'"

src = Path(target)
s = src.read_text()

s = s.replace(f"SAFE AGENT V{version}", f"SAFE AGENT V{next_version}")

if "STATE_BACKUP_FILE" not in s:
    s = s.replace(
        "STATE_FILE = Path(\"agent_state.json\")",
        (
            "STATE_FILE = Path(\"agent_state.json\")\n"
            "STATE_BACKUP_FILE = Path(\"agent_state.json.bak\")\n"
            "STATE_HASH_FILE = Path(\"agent_state.sha256\")\n"
            "AUDIT_LOG_FILE = Path(\"audit_log.jsonl\")"
        ),
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
        raise RuntimeError("FAIL-CLOSED: '\''version'\'' muss int sein")
    if not isinstance(state["tasks_completed"], int):
        raise RuntimeError("FAIL-CLOSED: '\''tasks_completed'\'' muss int sein")
    return state
"""
    marker = "def load_state():"
    if marker in s:
        s = s.replace(marker, validator.strip() + "\n\n\n" + marker, 1)
    else:
        s = s.rstrip() + "\n" + validator

new_helpers = ""
if "def calculate_state_hash" not in s:
    new_helpers += """
def calculate_state_hash(state):
    import hashlib
    import json as _json
    encoded = _json.dumps(state, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
"""
if "def audit_log" not in s:
    new_helpers += """
def audit_log(event, details=None):
    import json as _json
    import datetime as _dt
    entry = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event,
        "details": details or {},
    }
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
    encoded = json.dumps(state, indent=2)
    STATE_FILE.write_text(encoded, encoding="utf-8")
    STATE_HASH_FILE.write_text(calculate_state_hash(state), encoding="utf-8")
    audit_log("state_saved", {"tasks_completed": state.get("tasks_completed")})
"""
if save_state_pattern.search(s):
    s = save_state_pattern.sub(new_save_state, s, count=1)
else:
    s = s.rstrip() + "\n\n" + new_save_state

src.write_text(s, encoding="utf-8")
print(f"Patched: {target}")
'

echo -e "\n[7/12] Tests erzeugen"
cat << 'EOF' > tools/test_all.sh
#!/usr/bin/env bash
set -Eeuo pipefail
echo "== Syntax-Check =="
python3 -m py_compile safe_agent.py 2>/dev/null || python3 -m py_compile safe_agent_v*.py
echo "== Smoke-Test =="
TARGET_PY="$(ls safe_agent_v*.py | sort -V | tail -1)"
python3 "$TARGET_PY"
python3 "$TARGET_PY"
echo "== State-Dateien prüfen =="
for f in agent_state.json agent_state.json.bak agent_state.sha256; do
    if [ -f "$f" ]; then
        echo "OK: $f"
    else
        echo "FEHLT: $f"
        exit 1
    fi
done
echo "Alle Tests bestanden."
