"""
SAFE AGENT V50 Runtime Storage API

All mutable runtime storage must pass through this layer.
"""

from pathlib import Path

from .paths import (
    state_path,
    audit_path,
    snapshot_path,
)


def write_state(name: str, content: str):
    path = state_path(name)
    path.write_text(content)
    return path


def read_state(name: str):
    path = state_path(name)
    if not path.exists():
        return None
    return path.read_text()


def write_audit(name: str, content: str):
    path = audit_path(name)
    path.write_text(content)
    return path


def write_snapshot(name: str, content: str):
    path = snapshot_path(name)
    path.write_text(content)
    return path
