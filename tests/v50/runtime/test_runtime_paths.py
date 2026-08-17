from pathlib import Path

from autonomous_agent.runtime.paths import (
    STATE_ROOT,
    AUDIT_ROOT,
    SNAPSHOT_ROOT,
    CACHE_ROOT,
)


def test_runtime_layout_isolated():
    assert "state" in str(STATE_ROOT)
    assert "audit" in str(AUDIT_ROOT)
    assert "snapshots" in str(SNAPSHOT_ROOT)
    assert "cache" in str(CACHE_ROOT)


def test_runtime_not_source_root():
    assert STATE_ROOT.name == "state"
    assert AUDIT_ROOT.name == "audit"
    assert SNAPSHOT_ROOT.name == "snapshots"
    assert CACHE_ROOT.name == "cache"
