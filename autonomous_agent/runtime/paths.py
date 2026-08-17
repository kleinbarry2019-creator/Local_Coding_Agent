"""
SAFE AGENT V50 Runtime Path Abstraction

Central location for runtime-owned filesystem paths.

V50 goal:
- isolate mutable runtime data
- keep source tree immutable
- preserve V49 compatibility
"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

STATE_ROOT = PROJECT_ROOT / "state"
AUDIT_ROOT = STATE_ROOT / "audit"
SNAPSHOT_ROOT = STATE_ROOT / "snapshots"

RUNTIME_ROOT = PROJECT_ROOT / "runtime"
CACHE_ROOT = RUNTIME_ROOT / "cache"


def ensure_runtime_layout():
    """
    Create required runtime directories.
    """
    for path in (
        STATE_ROOT,
        AUDIT_ROOT,
        SNAPSHOT_ROOT,
        RUNTIME_ROOT,
        CACHE_ROOT,
    ):
        path.mkdir(parents=True, exist_ok=True)


def state_path(name: str) -> Path:
    """
    Resolve runtime state files.
    """
    ensure_runtime_layout()
    return STATE_ROOT / name


def audit_path(name: str) -> Path:
    """
    Resolve audit files.
    """
    ensure_runtime_layout()
    return AUDIT_ROOT / name


def snapshot_path(name: str) -> Path:
    """
    Resolve snapshot files.
    """
    ensure_runtime_layout()
    return SNAPSHOT_ROOT / name
