from autonomous_agent.runtime.storage import (
    write_state,
    write_audit,
    write_snapshot,
)


def test_state_storage_isolated(tmp_path):
    p = write_state("v50_test_state.txt", "OK")
    assert "state" in str(p)


def test_audit_storage_isolated(tmp_path):
    p = write_audit("v50_test_audit.txt", "OK")
    assert "audit" in str(p)


def test_snapshot_storage_isolated(tmp_path):
    p = write_snapshot("v50_test_snapshot.txt", "OK")
    assert "snapshots" in str(p)
