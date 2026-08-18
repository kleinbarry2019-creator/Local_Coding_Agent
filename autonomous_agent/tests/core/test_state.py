from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonomous_agent.core import state as state_module
from autonomous_agent.core.state import CoreStateStore, StateError

MIGRATION_1_SQL = """CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    current_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE config_snapshots (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _store(tmp_path: Path, *, busy_timeout_s: float = 2.0) -> CoreStateStore:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    return CoreStateStore(
        state_root / "agent_core.sqlite3",
        state_root / "audit_anchor.json",
        state_root / "audit.lock",
        busy_timeout_s=busy_timeout_s,
    )


def _insert_session(connection: sqlite3.Connection, session_id: str) -> None:
    connection.execute(
        """
        INSERT INTO sessions(session_id, mode, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, "monitored", "active", "2026-08-18T10:00:00Z", "2026-08-18T10:00:00Z"),
    )


def _assert_schema_drift_is_rejected(store: CoreStateStore) -> None:
    assert not store.verify_schema()
    with pytest.raises(StateError) as raised:
        store.initialize()
    assert raised.value.code == "schema_verification_failed"


def test_fresh_creation_has_exact_schema_and_owner_only_files(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.initialize()

    assert store.verify_schema()
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.database_path.parent.stat().st_mode) == 0o700
    with store.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == {
            "schema_migrations",
            "sessions",
            "events",
            "config_snapshots",
        }
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{store.database_path}{suffix}")
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_restart_persists_an_immutable_session_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as connection:
        _insert_session(connection, "session-1")

    restarted = CoreStateStore(
        store.database_path,
        store.anchor_path,
        store.lock_path,
    )
    restarted.initialize()

    session = restarted.load_session("session-1")
    assert session is not None
    assert session.session_id == "session-1"
    assert session.mode == "monitored"
    assert session.status == "active"
    assert session.created_at == "2026-08-18T10:00:00Z"
    assert session.updated_at == "2026-08-18T10:00:00Z"
    with pytest.raises(FrozenInstanceError):
        session.status = "changed"  # type: ignore[misc]


def test_migration_records_exact_sql_checksum_in_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()

    with store.connection() as connection:
        rows = connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert rows == [(1, hashlib.sha256(MIGRATION_1_SQL.encode()).hexdigest())]


def test_changed_migration_checksum_is_detected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("0" * 64,),
        )

    assert not store.verify_schema()
    with pytest.raises(StateError) as raised:
        store.initialize()
    assert raised.value.code == "schema_checksum_mismatch"


def test_out_of_order_migration_record_is_detected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO schema_migrations(version, checksum, applied_at)
            VALUES (3, ?, '2026-08-18T10:00:00Z')
            """,
            ("1" * 64,),
        )

    assert not store.verify_schema()
    with pytest.raises(StateError) as raised:
        store.initialize()
    assert raised.value.code == "schema_order_invalid"


@pytest.mark.parametrize("missing_unique", ["event_id", "current_hash"])
def test_missing_event_unique_constraint_is_schema_drift(
    tmp_path: Path, missing_unique: str
) -> None:
    store = _store(tmp_path)
    store.initialize()
    event_id = "event_id TEXT NOT NULL"
    current_hash = "current_hash TEXT NOT NULL"
    if missing_unique != "event_id":
        event_id += " UNIQUE"
    if missing_unique != "current_hash":
        current_hash += " UNIQUE"
    with store.connection() as connection:
        connection.execute("DROP TABLE events")
        connection.execute(
            f"""CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                {event_id},
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                {current_hash},
                created_at TEXT NOT NULL
            )"""
        )

    _assert_schema_drift_is_rejected(store)


def test_changed_event_foreign_key_action_is_schema_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute("DROP TABLE events")
        connection.execute(
            """CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id)
                    ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                current_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )"""
        )

    _assert_schema_drift_is_rejected(store)


def test_explicit_index_cannot_replace_event_unique_constraint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute("DROP TABLE events")
        connection.execute(
            """CREATE TABLE events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                current_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE UNIQUE INDEX replacement_event_id_unique ON events(event_id)"
        )

    _assert_schema_drift_is_rejected(store)


@pytest.mark.parametrize(
    "unexpected_sql",
    [
        "CREATE TABLE unexpected_table (value TEXT)",
        "CREATE INDEX unexpected_index ON sessions(status)",
        """CREATE TRIGGER unexpected_trigger
        AFTER INSERT ON sessions BEGIN SELECT 1; END""",
    ],
    ids=["table", "index", "trigger"],
)
def test_unexpected_schema_object_is_schema_drift(
    tmp_path: Path, unexpected_sql: str
) -> None:
    store = _store(tmp_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute(unexpected_sql)

    _assert_schema_drift_is_rejected(store)


def test_failed_migration_rolls_back_and_leaves_prior_schema_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.initialize()
    migration_2 = """CREATE TABLE migration_probe (value TEXT NOT NULL);
CREATE TABLE sessions (duplicate INTEGER);
"""
    monkeypatch.setattr(
        state_module,
        "_MIGRATIONS",
        ((1, MIGRATION_1_SQL), (2, migration_2)),
    )

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "migration_failed"
    with store.connection() as connection:
        applied = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        probe = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'migration_probe'"
        ).fetchone()
        _insert_session(connection, "still-usable")
    assert applied == [(1,)]
    assert probe is None
    assert store.load_session("still-usable") is not None


def test_every_connection_enables_wal_foreign_keys_and_bounded_timeout(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, busy_timeout_s=0.075)
    store.initialize()

    with store.connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (75,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO events(
                    sequence, event_id, session_id, event_type, payload_json,
                    previous_hash, current_hash, created_at
                ) VALUES (1, 'event-1', 'missing', 'test', '{}', 'GENESIS', 'hash', 'now')
                """
            )


def test_two_connections_report_database_busy_within_the_bound(tmp_path: Path) -> None:
    store = _store(tmp_path, busy_timeout_s=0.05)
    store.initialize()

    with store.connection() as first:
        first.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(StateError) as raised, store.connection() as second:
            second.execute("BEGIN IMMEDIATE")
        elapsed = time.monotonic() - started

    assert raised.value.code == "database_busy"
    assert 0.025 <= elapsed < 1.0


def test_database_symlink_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "target.sqlite3"
    target.touch(mode=0o600)
    store.database_path.symlink_to(target)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_database"


def test_database_fifo_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.mkfifo(store.database_path, mode=0o600)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_database"


@pytest.mark.parametrize("root_kind", ["symlink", "fifo"])
def test_non_directory_or_symlink_state_root_is_refused(
    tmp_path: Path, root_kind: str
) -> None:
    state_root = tmp_path / "state"
    if root_kind == "symlink":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        state_root.symlink_to(target, target_is_directory=True)
    else:
        os.mkfifo(state_root, mode=0o600)
    store = CoreStateStore(
        state_root / "agent_core.sqlite3",
        state_root / "audit_anchor.json",
        state_root / "audit.lock",
    )

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_state_directory"


def test_wrong_owner_state_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(state_module.os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_state_directory"


def test_wrong_owner_database_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.database_path.touch(mode=0o600)
    real_fstat = os.fstat

    def wrong_owner_fstat(descriptor: int) -> os.stat_result | SimpleNamespace:
        metadata = real_fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_uid=metadata.st_uid + 1,
                st_ino=metadata.st_ino,
                st_dev=metadata.st_dev,
            )
        return metadata

    monkeypatch.setattr(state_module.os, "fstat", wrong_owner_fstat)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_database"


@pytest.mark.parametrize("unsafe_target", ["state_root", "database"])
def test_group_or_other_writable_paths_are_refused(
    tmp_path: Path, unsafe_target: str
) -> None:
    store = _store(tmp_path)
    if unsafe_target == "state_root":
        store.database_path.parent.chmod(0o770)
        expected_code = "unsafe_state_directory"
    else:
        store.database_path.touch(mode=0o600)
        store.database_path.chmod(0o620)
        expected_code = "unsafe_database"

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == expected_code


@pytest.mark.parametrize("artifact_name", ["anchor_path", "lock_path"])
def test_existing_anchor_and_lock_must_be_owner_only_regular_files(
    tmp_path: Path, artifact_name: str
) -> None:
    store = _store(tmp_path)
    artifact = getattr(store, artifact_name)
    artifact.touch(mode=0o600)
    artifact.chmod(0o620)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_state_file"


def test_existing_sqlite_sidecar_must_be_an_owner_only_regular_file(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    sidecar = Path(f"{store.database_path}-wal")
    sidecar.touch(mode=0o600)
    sidecar.chmod(0o620)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_database_sidecar"


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_busy_timeout_is_rejected_before_database_creation(
    tmp_path: Path, timeout: float
) -> None:
    store = _store(tmp_path, busy_timeout_s=timeout)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "invalid_busy_timeout"
    assert not store.database_path.exists()


def test_database_identity_is_revalidated_after_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def swapping_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        displaced = store.database_path.with_suffix(".displaced")
        store.database_path.rename(displaced)
        store.database_path.touch(mode=0o600)
        return connection

    monkeypatch.setattr(state_module.sqlite3, "connect", swapping_connect)

    with pytest.raises(StateError) as raised:
        store.initialize()

    assert raised.value.code == "unsafe_database"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
