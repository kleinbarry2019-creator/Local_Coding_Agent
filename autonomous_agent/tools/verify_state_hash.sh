#!/usr/bin/env bash
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
