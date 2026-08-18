"""Secure SQLite state storage for the modular agent core."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_FILE_MODE = 0o600
_STATE_DIRECTORY_MODE = 0o700
_MAX_BUSY_TIMEOUT_S = 60.0

_MIGRATION_1_SQL = """CREATE TABLE schema_migrations (
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

# Migration SQL is immutable after release. Its exact UTF-8 bytes are checksummed.
_MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _MIGRATION_1_SQL),)

_EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    "schema_migrations": (
        ("version", "INTEGER", 0, 1),
        ("checksum", "TEXT", 1, 0),
        ("applied_at", "TEXT", 1, 0),
    ),
    "sessions": (
        ("session_id", "TEXT", 0, 1),
        ("mode", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "events": (
        ("sequence", "INTEGER", 0, 1),
        ("event_id", "TEXT", 1, 0),
        ("session_id", "TEXT", 1, 0),
        ("event_type", "TEXT", 1, 0),
        ("payload_json", "TEXT", 1, 0),
        ("previous_hash", "TEXT", 1, 0),
        ("current_hash", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
    "config_snapshots": (
        ("session_id", "TEXT", 0, 1),
        ("config_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
}


@dataclass
class StateError(Exception):
    """A stable, user-safe state storage failure."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    mode: str
    status: str
    created_at: str
    updated_at: str


class CoreStateStore:
    """Own the versioned core database beneath one validated state root."""

    def __init__(
        self,
        database_path: Path,
        anchor_path: Path,
        lock_path: Path,
        busy_timeout_s: float = 2.0,
    ) -> None:
        self.database_path = database_path
        self.anchor_path = anchor_path
        self.lock_path = lock_path
        self.busy_timeout_s = busy_timeout_s

    def initialize(self) -> None:
        """Create or migrate the database without exposing partial migrations."""
        plan = _migration_plan()
        with self.connection() as connection:
            applied = _read_applied_migrations(connection)
            _validate_applied_migrations(applied, plan)
            for version, migration_sql in plan[len(applied) :]:
                _apply_migration(connection, version, migration_sql)
            if not _schema_structure_matches(connection):
                raise StateError(
                    "schema_verification_failed",
                    "the core database schema does not match its migration record",
                )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and commit or roll back its transaction."""
        _busy_timeout_ms(self.busy_timeout_s)
        self._validate_layout()
        descriptor, prepared_metadata = self._prepare_database()
        connection: sqlite3.Connection | None = None
        try:
            try:
                connection = sqlite3.connect(
                    self.database_path,
                    timeout=self.busy_timeout_s,
                    isolation_level="DEFERRED",
                )
            except sqlite3.Error as error:
                raise _database_error(error) from error
            try:
                self._revalidate_database(prepared_metadata)
            except BaseException:
                connection.close()
                raise
        finally:
            os.close(descriptor)

        try:
            self._configure_connection(connection)
            self._validate_optional_files()
            try:
                yield connection
                if connection.in_transaction:
                    connection.commit()
                self._validate_sqlite_sidecars()
            except sqlite3.OperationalError as error:
                if connection.in_transaction:
                    connection.rollback()
                raise _database_error(error) from error
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        except sqlite3.OperationalError as error:
            if connection.in_transaction:
                connection.rollback()
            raise _database_error(error) from error
        except sqlite3.DatabaseError as error:
            if connection.in_transaction:
                connection.rollback()
            raise StateError("database_error", "the core database could not be used") from error
        finally:
            connection.close()

    def load_session(self, session_id: str) -> SessionRecord | None:
        """Load one immutable session record by its exact identifier."""
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, mode, status, created_at, updated_at
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SessionRecord(
            session_id=str(row[0]),
            mode=str(row[1]),
            status=str(row[2]),
            created_at=str(row[3]),
            updated_at=str(row[4]),
        )

    def verify_schema(self) -> bool:
        """Return whether migration records and the required schema agree."""
        plan = _migration_plan()
        with self.connection() as connection:
            try:
                applied = _read_applied_migrations(connection)
            except sqlite3.DatabaseError:
                return False
            expected = [
                (version, _migration_checksum(migration_sql))
                for version, migration_sql in plan
            ]
            return applied == expected and _schema_structure_matches(connection)

    def _validate_layout(self) -> None:
        paths = (self.database_path, self.anchor_path, self.lock_path)
        if any(not _is_absolute_normalized(path) for path in paths):
            raise StateError(
                "invalid_state_paths", "state file paths must be absolute and normalized"
            )
        state_root = self.database_path.parent
        if any(path.parent != state_root for path in paths[1:]):
            raise StateError(
                "invalid_state_paths", "state files must share one direct parent"
            )
        if len(set(paths)) != len(paths):
            raise StateError("invalid_state_paths", "state file paths must be distinct")

        sqlite_sidecars = {
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        }
        if self.anchor_path in sqlite_sidecars or self.lock_path in sqlite_sidecars:
            raise StateError(
                "invalid_state_paths", "audit paths must not alias SQLite sidecars"
            )
        _validate_state_directory(state_root)
        _validate_database_location(self.database_path)
        self._validate_optional_files()

    def _validate_optional_files(self) -> None:
        _validate_optional_owner_file(self.anchor_path, "unsafe_state_file")
        _validate_optional_owner_file(self.lock_path, "unsafe_state_file")
        self._validate_sqlite_sidecars()

    def _validate_sqlite_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            _validate_optional_owner_file(
                Path(f"{self.database_path}{suffix}"), "unsafe_database_sidecar"
            )

    def _prepare_database(self) -> tuple[int, os.stat_result]:
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        except AttributeError as error:
            raise StateError(
                "unsupported_platform",
                "secure database creation requires no-follow and close-on-exec flags",
            ) from error
        try:
            descriptor = os.open(self.database_path, flags, _FILE_MODE)
        except OSError as error:
            raise StateError(
                "unsafe_database", "the database could not be opened safely"
            ) from error

        try:
            metadata = os.fstat(descriptor)
            _validate_database_metadata(metadata)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, metadata

    def _revalidate_database(self, prepared_metadata: os.stat_result) -> None:
        try:
            current = self.database_path.lstat()
        except OSError as error:
            raise StateError(
                "unsafe_database", "the database identity changed while opening"
            ) from error
        _validate_database_metadata(current)
        if (current.st_dev, current.st_ino) != (
            prepared_metadata.st_dev,
            prepared_metadata.st_ino,
        ):
            raise StateError(
                "unsafe_database", "the database identity changed while opening"
            )

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        timeout_ms = _busy_timeout_ms(self.busy_timeout_s)
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "wal":
            raise StateError("database_error", "SQLite WAL mode is unavailable")
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys != (1,):
            raise StateError("database_error", "SQLite foreign keys are unavailable")


def _is_absolute_normalized(path: Path) -> bool:
    return path.is_absolute() and Path(os.path.normpath(str(path))) == path


def _validate_state_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise StateError(
            "unsafe_state_directory",
            "the validated state directory is missing or inaccessible",
        ) from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _STATE_DIRECTORY_MODE
    ):
        raise StateError(
            "unsafe_state_directory",
            "the state directory must be owner-controlled with mode 0700",
        )


def _validate_database_location(database_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[1]
    forbidden_roots = [package_root]
    for candidate in package_root.parents:
        if (candidate / ".git").exists():
            forbidden_roots.append(candidate)
            break
    if any(database_path.is_relative_to(root) for root in forbidden_roots):
        raise StateError(
            "database_location_forbidden",
            "the core database must not be stored in the repository or package",
        )


def _validate_optional_owner_file(path: Path, code: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise StateError(code, "a state file could not be inspected safely") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
    ):
        raise StateError(
            code, "state files must be owner-controlled regular files with mode 0600"
        )


def _validate_database_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
    ):
        raise StateError(
            "unsafe_database",
            "the database must be an owner-controlled regular file with mode 0600",
        )


def _busy_timeout_ms(timeout_s: float) -> int:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or not 0.0 < timeout_s <= _MAX_BUSY_TIMEOUT_S
    ):
        raise StateError(
            "invalid_busy_timeout",
            "the busy timeout must be positive, finite, and no greater than 60 seconds",
        )
    return max(1, math.ceil(timeout_s * 1000.0))


def _migration_plan() -> tuple[tuple[int, str], ...]:
    plan = tuple(_MIGRATIONS)
    versions = tuple(version for version, _migration_sql in plan)
    if versions != tuple(range(1, len(plan) + 1)):
        raise StateError(
            "invalid_migration_plan", "core migrations must be ordered and contiguous"
        )
    if any(not migration_sql for _version, migration_sql in plan):
        raise StateError("invalid_migration_plan", "core migration SQL must not be empty")
    checksums = tuple(
        _migration_checksum(migration_sql) for _version, migration_sql in plan
    )
    if len(set(checksums)) != len(checksums):
        raise StateError(
            "invalid_migration_plan", "core migration checksums must be unique"
        )
    return plan


def _migration_checksum(migration_sql: str) -> str:
    return hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()


def _read_applied_migrations(
    connection: sqlite3.Connection,
) -> list[tuple[int, str]]:
    present = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if present is None:
        return []
    rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def _validate_applied_migrations(
    applied: Sequence[tuple[int, str]], plan: Sequence[tuple[int, str]]
) -> None:
    for position, (version, checksum) in enumerate(applied, start=1):
        if version != position:
            raise StateError(
                "schema_order_invalid", "applied migrations are not ordered contiguously"
            )
        if position > len(plan):
            raise StateError(
                "unsupported_schema", "the database schema is newer than this program"
            )
        expected_checksum = _migration_checksum(plan[position - 1][1])
        if checksum != expected_checksum:
            raise StateError(
                "schema_checksum_mismatch",
                "an applied migration does not match its immutable SQL",
            )


def _apply_migration(
    connection: sqlite3.Connection, version: int, migration_sql: str
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        _execute_migration_sql(connection, migration_sql)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, checksum, applied_at)
            VALUES (?, ?, ?)
            """,
            (
                version,
                _migration_checksum(migration_sql),
                datetime.now(UTC).isoformat(timespec="microseconds"),
            ),
        )
        connection.commit()
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        if _is_database_busy(error):
            raise StateError(
                "database_busy", "the core database remained locked past its timeout"
            ) from error
        raise StateError(
            "migration_failed", f"core schema migration {version} failed"
        ) from error
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _execute_migration_sql(
    connection: sqlite3.Connection, migration_sql: str
) -> None:
    statement = ""
    for character in migration_sql:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        if not sqlite3.complete_statement(statement):
            raise StateError(
                "invalid_migration_plan", "core migration SQL is incomplete"
            )
        connection.execute(statement)


def _schema_structure_matches(connection: sqlite3.Connection) -> bool:
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not set(_EXPECTED_COLUMNS).issubset(tables):
            return False
        for table, expected_columns in _EXPECTED_COLUMNS.items():
            actual_columns = tuple(
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual_columns != expected_columns:
                return False
        event_foreign_keys = {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in connection.execute("PRAGMA foreign_key_list(events)")
        }
        snapshot_foreign_keys = {
            (str(row[2]), str(row[3]), str(row[4]))
            for row in connection.execute("PRAGMA foreign_key_list(config_snapshots)")
        }
    except sqlite3.DatabaseError:
        return False
    return event_foreign_keys == {("sessions", "session_id", "session_id")} and (
        snapshot_foreign_keys == {("sessions", "session_id", "session_id")}
    )


def _database_error(error: sqlite3.Error) -> StateError:
    if _is_database_busy(error):
        return StateError(
            "database_busy", "the core database remained locked past its timeout"
        )
    return StateError("database_error", "the core database could not be used")


def _is_database_busy(error: sqlite3.Error) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int) and error_code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(error).lower()
    return "locked" in message or "busy" in message
