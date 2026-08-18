from __future__ import annotations

import fcntl
import hashlib
import json
import multiprocessing
import os
import queue
import sqlite3
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from autonomous_agent.core import events as events_module
from autonomous_agent.core.config import (
    AgentConfig,
    ConfigSource,
    ExecutionMode,
    FieldProvenance,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.events import (
    AuditAnchor,
    AuditLog,
    AuditVerification,
    EventError,
    EventInput,
    EventRecord,
)
from autonomous_agent.core.state import CoreStateStore

CREATED_AT = "2026-08-18T10:00:00Z"
SECOND_AT = "2026-08-18T10:00:01Z"

type _Barrier = Any
type _Queue = Any


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "agent_core.sqlite3",
        root / "audit_anchor.json",
        root / "audit.lock",
    )


def _store(tmp_path: Path, *, busy_timeout_s: float = 2.0) -> CoreStateStore:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    store = CoreStateStore(*_paths(state_root), busy_timeout_s=busy_timeout_s)
    store.initialize()
    return store


def _config(tmp_path: Path) -> AgentConfig:
    paths = ResolvedPaths(
        config_file=tmp_path / "config.toml",
        project_file=tmp_path / "project" / ".local-agent.toml",
        project_root=tmp_path / "project",
        state_root=tmp_path / "state",
    )
    provenance = MappingProxyType(
        {
            "mode": FieldProvenance("mode", ConfigSource.CLI, None),
            "free_only": FieldProvenance("free_only", ConfigSource.BUILTIN, None),
        }
    )
    return AgentConfig(
        schema_version=1,
        mode=ExecutionMode.MONITORED,
        paths=paths,
        limits=ResourceLimits(),
        free_only=True,
        audit_required=True,
        provenance=provenance,
    )


def _start(log: AuditLog, tmp_path: Path, session_id: str = "session-1") -> EventRecord:
    return log.start_session(
        session_id,
        ExecutionMode.MONITORED,
        _config(tmp_path),
        CREATED_AT,
    )


def _event(
    *,
    event_id: str = "event-2",
    session_id: str = "session-1",
    event_type: str = "tool.failed",
    payload: Mapping[str, object] | None = None,
    created_at: str = SECOND_AT,
) -> EventInput:
    return EventInput(
        event_id=event_id,
        session_id=session_id,
        event_type=event_type,
        payload=(
            {
                "tool_name": "doctor.git",
                "status": "error",
                "diagnostic_code": "probe_failed",
                "incident_id": "incident-1",
                "duration_ms": 7,
                "truncated": False,
            }
            if payload is None
            else payload
        ),
        created_at=created_at,
    )


def _canonical_hash(
    sequence: int,
    event_id: str,
    session_id: str,
    event_type: str,
    payload: Mapping[str, object],
    previous_hash: str,
    created_at: str,
) -> str:
    document = {
        "sequence": sequence,
        "event_id": event_id,
        "session_id": session_id,
        "event_type": event_type,
        "payload": payload,
        "previous_hash": previous_hash,
        "created_at": created_at,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _anchor(store: CoreStateStore) -> dict[str, object]:
    value = json.loads(store.anchor_path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_anchor(store: CoreStateStore, value: Mapping[str, object]) -> None:
    store.anchor_path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    store.anchor_path.chmod(0o600)


def _rows(store: CoreStateStore) -> list[tuple[object, ...]]:
    with store.connection() as connection:
        return connection.execute(
            """
            SELECT sequence, event_id, session_id, event_type, payload_json,
                   previous_hash, current_hash, created_at
            FROM events ORDER BY sequence
            """
        ).fetchall()


def _crash_append(
    path_values: tuple[str, str, str],
    phase: str,
    exit_code: int,
) -> None:
    store = CoreStateStore(*(Path(value) for value in path_values))

    def crash(current_phase: str) -> None:
        if current_phase == phase:
            os._exit(exit_code)

    log = AuditLog(store, transition_hook=crash)
    log.append(_event())


def _paused_writer(
    path_values: tuple[str, str, str],
    phase: str,
    entered: _Barrier,
    release: _Barrier,
    result_queue: _Queue,
) -> None:
    store = CoreStateStore(*(Path(value) for value in path_values))

    def pause(current_phase: str) -> None:
        if current_phase == phase:
            entered.wait(timeout=10)
            release.wait(timeout=10)

    try:
        record = AuditLog(store, transition_hook=pause).append(
            _event(event_id=f"paused-{phase}")
        )
        result_queue.put(("paused", "ok", record.sequence))
    except Exception as error:  # noqa: BLE001 - report child failures to parent
        result_queue.put(("paused", "error", type(error).__name__))


def _competing_writer(
    path_values: tuple[str, str, str],
    phase: str,
    start: _Barrier,
    result_queue: _Queue,
) -> None:
    store = CoreStateStore(*(Path(value) for value in path_values))
    start.wait(timeout=10)
    result_queue.put(("writer", "attempt", phase))
    try:
        record = AuditLog(store).append(_event(event_id=f"competing-{phase}"))
        result_queue.put(("writer", "ok", record.sequence))
    except Exception as error:  # noqa: BLE001 - report child failures to parent
        result_queue.put(("writer", "error", type(error).__name__))


def _competing_verifier(
    path_values: tuple[str, str, str],
    phase: str,
    start: _Barrier,
    result_queue: _Queue,
) -> None:
    store = CoreStateStore(*(Path(value) for value in path_values))
    start.wait(timeout=10)
    result_queue.put(("verifier", "attempt", phase))
    try:
        verification = AuditLog(store).verify()
        result_queue.put(("verifier", verification.code, verification.sequence))
    except Exception as error:  # noqa: BLE001 - report child failures to parent
        result_queue.put(("verifier", "error", type(error).__name__))


def _hold_lock(path: str, ready: _Barrier, release: _Barrier) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.wait(timeout=10)
        release.wait(timeout=10)
    finally:
        os.close(descriptor)


def _join(process: multiprocessing.Process) -> None:
    process.join(timeout=10)
    assert not process.is_alive()


def test_public_event_results_are_immutable() -> None:
    anchor = AuditAnchor(sequence=0, hash="GENESIS")
    event = _event()
    record = EventRecord(
        1,
        event.event_id,
        event.session_id,
        event.event_type,
        event.payload,
        "GENESIS",
        "0" * 64,
        event.created_at,
    )
    verification = AuditVerification(True, "ok", 1, "0" * 64)

    with pytest.raises(FrozenInstanceError):
        anchor.sequence = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        event.event_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.sequence = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        verification.ok = False  # type: ignore[misc]


def test_first_session_starts_at_genesis_and_commits_state_atomically(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    record = _start(AuditLog(store), tmp_path)

    assert record.sequence == 1
    assert record.previous_hash == "GENESIS"
    assert record.event_type == "session.created"
    assert record.payload == {"mode": "monitored", "status": "active"}
    expected_hash = _canonical_hash(
        1,
        "session.created:session-1",
        "session-1",
        "session.created",
        {"mode": "monitored", "status": "active"},
        "GENESIS",
        CREATED_AT,
    )
    assert record.current_hash == expected_hash
    assert _anchor(store) == {
        "version": 1,
        "committed": {"sequence": 1, "hash": expected_hash},
        "pending": None,
    }
    assert stat.S_IMODE(store.anchor_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600
    assert store.load_session("session-1") is not None
    with store.connection() as connection:
        snapshot = connection.execute(
            "SELECT config_json FROM config_snapshots WHERE session_id = ?",
            ("session-1",),
        ).fetchone()
    assert snapshot is not None
    assert json.loads(str(snapshot[0])) == _config(tmp_path).redacted_dict()


def test_restart_continues_one_global_chain_and_verifies_from_genesis(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _start(AuditLog(store), tmp_path)

    restarted = CoreStateStore(
        store.database_path,
        store.anchor_path,
        store.lock_path,
    )
    second = AuditLog(restarted).append(_event())

    assert second.sequence == 2
    assert second.previous_hash == first.current_hash
    assert AuditLog(restarted).verify() == AuditVerification(
        True, "ok", 2, second.current_hash
    )


def test_hash_uses_exact_canonical_utf8_event_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    payload = {
        "status": "error",
        "tool_name": "döctor",
        "duration_ms": 3,
        "diagnostic_code": "échec",
    }

    record = AuditLog(store).append(_event(payload=payload))

    assert record.current_hash == _canonical_hash(
        2,
        "event-2",
        "session-1",
        "tool.failed",
        payload,
        record.previous_hash,
        SECOND_AT,
    )
    row = _rows(store)[1]
    assert row[4] == json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        (
            "session.created",
            {"mode": "autonomous", "status": "active", "raw_env": "drop"},
            {"mode": "autonomous", "status": "active"},
        ),
        (
            "config.snapshot",
            {
                "schema_version": 1,
                "mode": "monitored",
                "paths": {"state_root": "/safe", "unknown": "drop"},
                "limits": {"max_output_bytes": 4096, "mystery": 99},
                "free_only": True,
                "audit_required": True,
                "provenance": {
                    "mode": {
                        "field": "mode",
                        "source": "cli",
                        "source_path": None,
                        "extra": "drop",
                    },
                    "unknown": {"field": "unknown", "source": "project"},
                },
                "environment": {"HOME": "/private"},
            },
            {
                "schema_version": 1,
                "mode": "monitored",
                "paths": {"state_root": "/safe"},
                "limits": {"max_output_bytes": 4096},
                "free_only": True,
                "audit_required": True,
                "provenance": {
                    "mode": {
                        "field": "mode",
                        "source": "cli",
                        "source_path": None,
                    }
                },
            },
        ),
        (
            "tool.failed",
            {
                "tool_name": "doctor.git",
                "status": "error",
                "diagnostic_code": "failed",
                "incident_id": "incident-7",
                "duration_ms": 5,
                "truncated": True,
                "output": "raw output",
                "exception": "raw exception",
                "model_text": "raw model text",
                "payload": {"arbitrary": True},
            },
            {
                "tool_name": "doctor.git",
                "status": "error",
                "diagnostic_code": "failed",
                "incident_id": "incident-7",
                "duration_ms": 5,
                "truncated": True,
            },
        ),
        (
            "audit.recovered",
            {
                "action": "pending_finalized",
                "code": "ok",
                "recovered_sequence": 2,
                "database_dump": "drop",
            },
            {
                "action": "pending_finalized",
                "code": "ok",
                "recovered_sequence": 2,
            },
        ),
    ],
)
def test_event_specific_allowlists_drop_unknown_fields(
    tmp_path: Path,
    event_type: str,
    payload: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)

    record = AuditLog(store).append(_event(event_type=event_type, payload=payload))

    assert record.payload == expected
    persisted = json.loads(str(_rows(store)[1][4]))
    assert persisted == expected


def test_unknown_event_type_fails_closed_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    head = _start(AuditLog(store), tmp_path)

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(_event(event_type="model.transcript"))

    assert raised.value.code == "redaction_failed"
    assert len(_rows(store)) == 1
    assert _anchor(store)["committed"] == {
        "sequence": 1,
        "hash": head.current_hash,
    }


def test_persistence_boundary_recursively_redacts_keys_and_exact_values(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    secret = "exact-value-should-never-persist"
    payload = {
        "schema_version": 1,
        "mode": "monitored",
        "paths": {
            "state_root": f"/safe/{secret}",
            "config_file": "/safe/config",
        },
        "limits": {"max_output_bytes": 4096},
        "provenance": {
            "mode": {
                "field": "mode",
                "source": "cli",
                "source_path": secret,
            }
        },
        "nested": {"Api-Key": secret},
        "clientAuthorizationToken": secret,
    }

    record = AuditLog(store).append(
        _event(event_type="config.snapshot", payload=payload)
    )

    serialized = json.dumps(record.payload, sort_keys=True)
    database_bytes = store.database_path.read_bytes()
    assert secret not in serialized
    assert secret.encode() not in database_bytes
    assert record.payload["paths"] == {
        "config_file": "/safe/config",
        "state_root": "/safe/[REDACTED]",
    }
    provenance = record.payload["provenance"]
    assert isinstance(provenance, Mapping)
    mode = provenance["mode"]
    assert isinstance(mode, Mapping)
    assert mode["source_path"] == "[REDACTED]"


def test_config_collected_exact_secrets_redact_later_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    secret = "ordinary-collected-value-with-no-token-pattern"
    config = _config(tmp_path)
    object.__setattr__(config, "_sensitive_values", (secret,))
    log = AuditLog(store)
    log.start_session("session-1", ExecutionMode.MONITORED, config, CREATED_AT)

    record = log.append(
        _event(payload={"tool_name": f"doctor {secret}", "status": "error"})
    )

    assert record.payload["tool_name"] == "doctor [REDACTED]"
    assert secret not in str(_rows(store)[1][4])


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "Bearer abcdefghijklmnopqrstuvwxyz012345",
        "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
        "https://user:supersecret@example.invalid/path",
        "password=hunter2-secret",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-part",
    ],
)
def test_value_patterns_are_redacted_before_persistence(
    tmp_path: Path, unsafe_value: str
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)

    record = AuditLog(store).append(
        _event(payload={"tool_name": unsafe_value, "status": "error"})
    )

    assert unsafe_value not in json.dumps(record.payload)
    assert "[REDACTED]" in str(record.payload["tool_name"])


@pytest.mark.parametrize("unsafe_value", [object(), {"nested": object()}, float("nan")])
def test_non_json_allowed_payload_fails_before_pending_anchor(
    tmp_path: Path, unsafe_value: object
) -> None:
    store = _store(tmp_path)
    first = _start(AuditLog(store), tmp_path)

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(
            _event(payload={"tool_name": unsafe_value, "status": "error"})
        )

    assert raised.value.code == "redaction_failed"
    assert len(_rows(store)) == 1
    assert _anchor(store)["pending"] is None
    assert _anchor(store)["committed"] == {
        "sequence": 1,
        "hash": first.current_hash,
    }


def test_event_and_state_mutation_commit_in_one_sqlite_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)

    record = AuditLog(store).append(
        _event(),
        lambda connection: connection.execute(
            "UPDATE sessions SET status = 'failed' WHERE session_id = 'session-1'"
        ),
    )

    assert record.sequence == 2
    session = store.load_session("session-1")
    assert session is not None
    assert session.status == "failed"
    assert AuditLog(store).verify().ok


def test_state_mutation_failure_rolls_back_event_and_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _start(AuditLog(store), tmp_path)

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE sessions SET status = 'failed' WHERE session_id = 'session-1'"
        )
        raise RuntimeError("raw mutation detail")

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(_event(), fail)

    assert raised.value.code == "transaction_failed"
    assert "raw mutation detail" not in str(raised.value)
    session = store.load_session("session-1")
    assert session is not None
    assert session.status == "active"
    assert len(_rows(store)) == 1
    assert _anchor(store) == {
        "version": 1,
        "committed": {"sequence": 1, "hash": first.current_hash},
        "pending": None,
    }


def test_state_mutation_cannot_silently_end_the_shared_transaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _start(AuditLog(store), tmp_path)

    def rollback(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE sessions SET status = 'failed' WHERE session_id = 'session-1'"
        )
        connection.rollback()

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(_event(), rollback)

    assert raised.value.code == "transaction_failed"
    session = store.load_session("session-1")
    assert session is not None
    assert session.status == "active"
    assert len(_rows(store)) == 1
    assert _anchor(store) == {
        "version": 1,
        "committed": {"sequence": 1, "hash": first.current_hash},
        "pending": None,
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            "UPDATE events SET event_type = 'config.snapshot' WHERE sequence = 1",
            "hash_mismatch",
        ),
        ("DELETE FROM events WHERE sequence = 1", "sequence_mismatch"),
        (
            "UPDATE events SET previous_hash = 'GENESIS' WHERE sequence = 2",
            "hash_mismatch",
        ),
    ],
    ids=["content", "deletion", "competing-link"],
)
def test_verification_detects_mutation_deletion_and_competing_links(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    AuditLog(store).append(_event())

    with store.connection() as connection:
        connection.execute(mutation)

    verification = AuditLog(store).verify()
    assert not verification.ok
    assert verification.code == expected_code


def test_verification_detects_reordered_sequences(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    AuditLog(store).append(_event())
    with store.connection() as connection:
        connection.execute("UPDATE events SET sequence = 99 WHERE sequence = 1")
        connection.execute("UPDATE events SET sequence = 1 WHERE sequence = 2")
        connection.execute("UPDATE events SET sequence = 2 WHERE sequence = 99")

    verification = AuditLog(store).verify()

    assert not verification.ok
    assert verification.code == "hash_mismatch"


def test_verification_detects_tail_truncation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    AuditLog(store).append(_event())
    with store.connection() as connection:
        connection.execute("DELETE FROM events WHERE sequence = 2")

    verification = AuditLog(store).verify()

    assert not verification.ok
    assert verification.code == "tail_mismatch"


def test_verification_detects_missing_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    store.anchor_path.unlink()

    verification = AuditLog(store).verify()

    assert verification == AuditVerification(False, "anchor_missing", 0, "GENESIS")


def test_verification_detects_anchor_hash_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    value = _anchor(store)
    committed = value["committed"]
    assert isinstance(committed, dict)
    committed["hash"] = "f" * 64
    _write_anchor(store, value)

    verification = AuditLog(store).verify()

    assert not verification.ok
    assert verification.code == "anchor_mismatch"


def test_database_tail_without_pending_fails_closed_and_is_not_repaired(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _start(AuditLog(store), tmp_path)
    payload = {"status": "error", "tool_name": "doctor.git"}
    second_hash = _canonical_hash(
        2,
        "manual-tail",
        "session-1",
        "tool.failed",
        payload,
        first.current_hash,
        SECOND_AT,
    )
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO events(
                sequence, event_id, session_id, event_type, payload_json,
                previous_hash, current_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                "manual-tail",
                "session-1",
                "tool.failed",
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                first.current_hash,
                second_hash,
                SECOND_AT,
            ),
        )

    verification = AuditLog(store).recover_pending()

    assert not verification.ok
    assert verification.code == "tail_mismatch"
    assert _anchor(store)["committed"] == {
        "sequence": 1,
        "hash": first.current_hash,
    }
    assert len(_rows(store)) == 2


@pytest.mark.parametrize(
    ("phase", "expected_rows", "expected_code"),
    [
        ("before_pending", 1, "ok"),
        ("after_pending", 1, "pending_cleared"),
        ("after_sqlite_commit", 2, "pending_finalized"),
        ("after_sidecar_finalization", 2, "ok"),
    ],
)
def test_real_process_death_at_each_transition_recovers_deterministically(
    tmp_path: Path,
    phase: str,
    expected_rows: int,
    expected_code: str,
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_append,
        args=(
            tuple(str(path) for path in _paths(store.database_path.parent)),
            phase,
            73,
        ),
    )

    process.start()
    _join(process)

    assert process.exitcode == 73
    recovery = AuditLog(store).recover_pending()
    assert recovery.ok
    assert recovery.code == expected_code
    assert recovery.sequence == expected_rows
    assert len(_rows(store)) == expected_rows
    assert _anchor(store)["pending"] is None
    assert AuditLog(store).verify().ok


def test_pending_hash_mismatch_fails_closed_without_repair(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_append,
        args=(
            tuple(str(path) for path in _paths(store.database_path.parent)),
            "after_sqlite_commit",
            74,
        ),
    )
    process.start()
    _join(process)
    assert process.exitcode == 74
    with store.connection() as connection:
        connection.execute(
            "UPDATE events SET current_hash = ? WHERE sequence = 2",
            ("e" * 64,),
        )
    before = store.anchor_path.read_bytes()

    verification = AuditLog(store).recover_pending()

    assert not verification.ok
    assert verification.code == "hash_mismatch"
    assert store.anchor_path.read_bytes() == before


def test_pending_with_extra_database_row_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_append,
        args=(
            tuple(str(path) for path in _paths(store.database_path.parent)),
            "after_sqlite_commit",
            75,
        ),
    )
    process.start()
    _join(process)
    assert process.exitcode == 75
    second = _rows(store)[1]
    third_payload = {"status": "error", "tool_name": "doctor.third"}
    third_hash = _canonical_hash(
        3,
        "extra-tail",
        "session-1",
        "tool.failed",
        third_payload,
        str(second[6]),
        "2026-08-18T10:00:02Z",
    )
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO events(
                sequence, event_id, session_id, event_type, payload_json,
                previous_hash, current_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                3,
                "extra-tail",
                "session-1",
                "tool.failed",
                json.dumps(third_payload, sort_keys=True, separators=(",", ":")),
                str(second[6]),
                third_hash,
                "2026-08-18T10:00:02Z",
            ),
        )
    before = store.anchor_path.read_bytes()

    verification = AuditLog(store).recover_pending()

    assert not verification.ok
    assert verification.code == "tail_mismatch"
    assert store.anchor_path.read_bytes() == before
    assert len(_rows(store)) == 3


@pytest.mark.parametrize(
    "phase",
    [
        "before_pending",
        "after_pending",
        "after_sqlite_commit",
        "after_sidecar_finalization",
    ],
)
def test_two_writers_and_verifier_serialize_at_every_transition(
    tmp_path: Path, phase: str
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    path_values = tuple(str(path) for path in _paths(store.database_path.parent))
    context = multiprocessing.get_context("spawn")
    entered = context.Barrier(2)
    release = context.Barrier(2)
    start = context.Barrier(3)
    results = context.Queue()
    paused = context.Process(
        target=_paused_writer,
        args=(path_values, phase, entered, release, results),
    )
    writer = context.Process(
        target=_competing_writer,
        args=(path_values, phase, start, results),
    )
    verifier = context.Process(
        target=_competing_verifier,
        args=(path_values, phase, start, results),
    )

    paused.start()
    entered.wait(timeout=10)
    writer.start()
    verifier.start()
    start.wait(timeout=10)
    attempts = {results.get(timeout=10)[:2], results.get(timeout=10)[:2]}
    assert attempts == {("writer", "attempt"), ("verifier", "attempt")}
    time.sleep(0.1)
    assert writer.is_alive()
    assert verifier.is_alive()
    with pytest.raises(queue.Empty):
        results.get_nowait()
    release.wait(timeout=10)

    _join(paused)
    _join(writer)
    _join(verifier)
    assert paused.exitcode == writer.exitcode == verifier.exitcode == 0
    outcomes = [results.get(timeout=10) for _ in range(3)]
    assert ("paused", "ok", 2) in outcomes
    assert any(item[:2] == ("writer", "ok") for item in outcomes)
    assert any(item[:2] == ("verifier", "ok") for item in outcomes)
    assert AuditLog(store).verify().sequence == 3


def test_lock_file_symlink_is_rejected_without_following(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    store.lock_path.symlink_to(target)

    with pytest.raises(EventError) as raised:
        _start(AuditLog(store), tmp_path)

    assert raised.value.code == "unsafe_lock_file"
    assert target.read_text(encoding="utf-8") == "untouched"


def test_lock_file_unsafe_mode_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.lock_path.write_text("", encoding="utf-8")
    store.lock_path.chmod(0o640)

    with pytest.raises(EventError) as raised:
        _start(AuditLog(store), tmp_path)

    assert raised.value.code == "unsafe_lock_file"
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o640


def test_lock_file_wrong_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.lock_path.write_text("", encoding="utf-8")
    store.lock_path.chmod(0o600)
    actual_uid = os.getuid()
    monkeypatch.setattr(events_module.os, "getuid", lambda: actual_uid + 1)

    with pytest.raises(EventError) as raised:
        _start(AuditLog(store), tmp_path)

    assert raised.value.code == "unsafe_lock_file"


def test_cross_process_lock_timeout_has_stable_code(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(2)
    release = context.Barrier(2)
    holder = context.Process(
        target=_hold_lock,
        args=(str(store.lock_path), ready, release),
    )
    holder.start()
    ready.wait(timeout=10)
    try:
        with pytest.raises(EventError) as raised:
            AuditLog(store, lock_timeout_s=0.05).append(_event())
        assert raised.value.code == "lock_timeout"
    finally:
        release.wait(timeout=10)
        _join(holder)
    assert holder.exitcode == 0


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode"])
def test_unsafe_anchor_file_is_rejected(tmp_path: Path, unsafe_kind: str) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    if unsafe_kind == "symlink":
        store.anchor_path.unlink()
        target = tmp_path / "anchor-target"
        target.write_text("{}", encoding="utf-8")
        store.anchor_path.symlink_to(target)
    else:
        store.anchor_path.chmod(0o644)

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(_event())

    assert raised.value.code == "unsafe_anchor_file"


@pytest.mark.parametrize(
    "anchor_value",
    [
        {},
        {
            "version": 2,
            "committed": {"sequence": 0, "hash": "GENESIS"},
            "pending": None,
        },
        {
            "version": 1,
            "committed": {"sequence": 0, "hash": "GENESIS"},
            "pending": {"sequence": 2, "hash": "a" * 64},
        },
        {
            "version": 1,
            "committed": {"sequence": True, "hash": "GENESIS"},
            "pending": None,
        },
    ],
)
def test_malformed_anchor_fails_closed(
    tmp_path: Path, anchor_value: Mapping[str, object]
) -> None:
    store = _store(tmp_path)
    _write_anchor(store, anchor_value)

    verification = AuditLog(store).verify()

    assert not verification.ok
    assert verification.code == "anchor_invalid"


def test_in_process_mutex_serializes_distinct_log_instances(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)
    entered = threading.Barrier(2)
    release = threading.Barrier(2)
    attempted = threading.Event()
    results: queue.Queue[tuple[str, int]] = queue.Queue()

    def pause(phase: str) -> None:
        if phase == "after_pending":
            entered.wait(timeout=10)
            release.wait(timeout=10)

    def first_writer() -> None:
        record = AuditLog(store, transition_hook=pause).append(
            _event(event_id="thread-seam-1")
        )
        results.put(("first", record.sequence))

    def second_writer() -> None:
        attempted.set()
        record = AuditLog(store).append(_event(event_id="thread-seam-2"))
        results.put(("second", record.sequence))

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    entered.wait(timeout=10)
    second.start()
    assert attempted.wait(timeout=10)
    time.sleep(0.05)
    assert second.is_alive()
    assert results.empty()
    release.wait(timeout=10)
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert {results.get(timeout=1), results.get(timeout=1)} == {
        ("first", 2),
        ("second", 3),
    }
    assert AuditLog(store).verify().sequence == 3


def test_append_rejects_invalid_utc_timestamp_before_any_transition(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(_event(created_at="2026-08-18T10:00:01"))

    assert raised.value.code == "invalid_event"
    assert len(_rows(store)) == 1


def test_event_error_is_user_safe_and_has_no_raw_sqlite_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _start(AuditLog(store), tmp_path)

    with pytest.raises(EventError) as raised:
        AuditLog(store).append(_event(event_id="session.created:session-1"))

    assert raised.value.code == "transaction_failed"
    assert "UNIQUE" not in str(raised.value)
    assert "events.event_id" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
