#!/usr/bin/env python3

"""
SAFE AGENT V50 Builder

Generates SAFE AGENT runtime code.

V50 changes:
- centralized runtime storage
- isolated state/audit/snapshot paths
- no direct mutable file writers
"""

from pathlib import Path
import re


ROOT = Path(__file__).parent
AGENT_FILE = ROOT / "autonomous_agent.py"


def migrate_version(source: str):
    version = 49
    next_version = 50

    return source.replace(
        f"SAFE AGENT V{version}",
        f"SAFE AGENT V{next_version}"
    )


def inject_runtime_imports(source: str):

    marker = "from runtime.paths import"

    if marker in source:
        return source

    runtime_import = """

from runtime.storage import (
    write_state,
    read_state,
    write_audit,
)

"""

    return runtime_import + source


def migrate_storage(source: str):

    replacements = {

        'STATE_FILE = Path("agent_state.json")':
        'STATE_FILE = None',

        'STATE_BACKUP_FILE = Path("agent_state.json.bak")':
        'STATE_BACKUP_FILE = None',

        'STATE_HASH_FILE = Path("agent_state.sha256")':
        'STATE_HASH_FILE = None',

        'AUDIT_LOG_FILE = Path("audit_log.jsonl")':
        'AUDIT_LOG_FILE = None',
    }

    for old, new in replacements.items():
        source = source.replace(old, new)

    return source


def migrate_save_state(source: str):

    pattern = re.compile(
        r"def save_state\(state\):.*?(?=\ndef |\Z)",
        re.MULTILINE | re.DOTALL
    )

    replacement = r'''def save_state(state):
    """
    V50 centralized state writer
    """

    import json

    write_state(
        "agent_state.json",
        json.dumps(
            state,
            indent=2
        )
    )

    write_audit(
        "audit_log.jsonl",
        json.dumps(
            {
                "event": "state_saved",
                "tasks_completed":
                    state.get("tasks_completed")
            }
        )
    )

'''

    return pattern.sub(
        replacement,
        source,
        count=1
    )


def build():

    if not AGENT_FILE.exists():
        raise RuntimeError(
            f"Missing agent source: {AGENT_FILE}"
        )

    source = AGENT_FILE.read_text(
        encoding="utf-8"
    )

    source = migrate_version(source)

    source = inject_runtime_imports(
        source
    )

    source = migrate_storage(
        source
    )

    source = migrate_save_state(
        source
    )

    AGENT_FILE.write_text(
        source,
        encoding="utf-8"
    )

    print(
        "SAFE AGENT V50 BUILD COMPLETE"
    )


if __name__ == "__main__":
    build()
