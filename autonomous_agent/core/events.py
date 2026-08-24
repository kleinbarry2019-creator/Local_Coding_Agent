"""Locked, tamper-evident audit events for the modular agent core."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from autonomous_agent.core.config import (
    AgentConfig,
    ConfigSource,
    ExecutionMode,
    ResourceLimits,
)
from autonomous_agent.core.state import CoreStateStore, StateError

_ANCHOR_VERSION = 1
_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_GENESIS_HASH = "GENESIS"
_MAX_LOCK_TIMEOUT_S = 60.0
_MAX_ANCHOR_BYTES = 4096
_MAX_CONTAINER_ITEMS = 256
_MAX_DEPTH = 16
_MAX_EVENT_TEXT_BYTES = 1024
_MAX_IDENTIFIER_BYTES = 256
_MAX_PATH_BYTES = 4096
_MAX_STRING_BYTES = 65_536
_MAX_CANONICAL_BYTES = 1_048_576
_MAX_DURATION_MS = 86_400_000
_MAX_CONFIG_INTEGER = 2**63 - 1
_MAX_CONFIG_TIMEOUT_S = 86_400.0
_REDACTED = "[REDACTED]"

_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "token",
    "secret",
    "apikey",
    "authorization",
    "auth",
    "credential",
)
_SESSION_EVENT_FIELDS = frozenset({"mode", "status"})
_TOOL_FAILED_EVENT_FIELDS = frozenset(
    {
        "tool_name",
        "status",
        "diagnostic_code",
        "incident_id",
        "duration_ms",
        "truncated",
    }
)
_AUDIT_RECOVERED_EVENT_FIELDS = frozenset({"action", "code", "recovered_sequence"})
_TASK_EVENT_FIELDS = frozenset(
    {"status", "step_index", "attempts", "outcome", "completion"}
)
_CHECKPOINT_EVENT_FIELDS = frozenset({"checkpoint_id", "step_id", "status"})
_CAPABILITY_EVENT_FIELDS = frozenset({"name", "kind", "source", "available"})
_TASK_STATUSES = frozenset(
    {"pending", "running", "recovering", "blocked", "failed", "completed"}
)
_CHECKPOINT_STATUSES = frozenset({"created", "restored", "discarded"})
_CONFIG_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "paths",
        "limits",
        "free_only",
        "audit_required",
        "provenance",
    }
)
_CONFIG_PATH_FIELDS = frozenset(
    {"config_file", "project_file", "project_root", "state_root"}
)
_CONFIG_LIMIT_FIELDS = frozenset(ResourceLimits.__dataclass_fields__)
_CONFIG_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "project_root",
        "state_dir",
        "free_only",
        "audit_required",
        *_CONFIG_LIMIT_FIELDS,
    }
)
_CONFIG_PROVENANCE_FIELDS = frozenset({"field", "source", "source_path"})
_CONFIG_INTEGER_LIMIT_FIELDS = frozenset(
    {
        "max_output_bytes",
        "hard_max_output_bytes",
        "min_free_ram_mib",
        "min_free_disk_mib",
    }
)
_CONFIG_TIMEOUT_FIELDS = frozenset(
    {"command_timeout_s", "hard_command_timeout_s", "doctor_probe_timeout_s"}
)
_CONFIG_PERCENT_FIELDS = frozenset({"max_cpu_percent", "max_vram_percent"})
_EXECUTION_MODE_VALUES = frozenset(mode.value for mode in ExecutionMode)
_CONFIG_SOURCE_VALUES = frozenset(source.value for source in ConfigSource)
_AUDIT_RECOVERY_ACTIONS = frozenset({"pending_cleared", "pending_finalized"})
_AUDIT_RECOVERY_CODES = frozenset({"ok", "pending_cleared", "pending_finalized"})
_PERSISTED_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

_PATTERN_SAFEGUARDS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s@]+@"),
    re.compile(
        r"(?i)\b(?:password|passwd|token|secret|api[_-]?key|apikey|auth|"
        r"authorization|credential)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

_MUTEXES_GUARD = threading.Lock()
_MUTEXES: dict[str, threading.Lock] = {}
_ACTIVE_LOCK_FDS_GUARD = threading.Lock()
_ACTIVE_LOCK_FDS: set[int] = set()
_MUTATION_CONTEXT = threading.local()

type _JsonValue = (
    str | int | float | bool | None | dict[str, "_JsonValue"] | list["_JsonValue"]
)
type _DatabaseRow = tuple[int, str, str, str, str, str, str, str]
type _TransitionHook = Callable[[str], None]
type _SqlParameters = Sequence[Any] | Mapping[str, Any]


@dataclass
class EventError(Exception):
    """A stable, user-safe audit failure."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class StateMutationResult:
    """Non-escaping metadata returned by a restricted state DML operation."""

    rowcount: int
    lastrowid: int | None


class StateTransaction:
    """Restricted audited transaction capability exposing state DML only."""

    __slots__ = ()

    def execute(
        self, statement: str, parameters: _SqlParameters = (), /
    ) -> StateMutationResult:
        """Execute one INSERT, UPDATE, or DELETE against an allowed state table."""
        connection = getattr(_MUTATION_CONTEXT, "connection", None)
        if not isinstance(connection, sqlite3.Connection):
            raise sqlite3.ProgrammingError("the audited transaction is not active")
        return _execute_state_dml(connection, statement, parameters)


@dataclass(frozen=True)
class AuditAnchor:
    """One sequence/hash pair in the external audit anchor."""

    sequence: int
    hash: str


@dataclass(frozen=True)
class EventInput:
    event_id: str
    session_id: str
    event_type: str
    payload: Mapping[str, object]
    created_at: str


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    event_id: str
    session_id: str
    event_type: str
    payload: Mapping[str, object]
    previous_hash: str
    current_hash: str
    created_at: str


@dataclass(frozen=True)
class AuditVerification:
    ok: bool
    code: str
    sequence: int
    head_hash: str


@dataclass(frozen=True)
class _AnchorState:
    version: int
    committed: AuditAnchor
    pending: AuditAnchor | None


class AuditLog:
    """Serialize event writes and verify one global database-backed hash chain."""

    def __init__(
        self,
        store: CoreStateStore,
        *,
        lock_timeout_s: float | None = None,
        sensitive_values: Sequence[str] = (),
        transition_hook: _TransitionHook | None = None,
    ) -> None:
        self._store = store
        self._lock_timeout_s = _validated_lock_timeout(
            store.busy_timeout_s if lock_timeout_s is None else lock_timeout_s
        )
        self._sensitive_values = _validated_sensitive_values(sensitive_values)
        self._transition_hook = transition_hook

    def start_session(
        self,
        session_id: str,
        mode: ExecutionMode,
        config: AgentConfig,
        created_at: str,
    ) -> EventRecord:
        """Atomically persist a new session, config snapshot, and audit event."""
        if not isinstance(mode, ExecutionMode):
            raise EventError("invalid_event", "the session mode is invalid")
        if config.mode is not mode:
            raise EventError(
                "invalid_event", "the session mode and configuration mode differ"
            )
        discovered_values = getattr(config, "_sensitive_values", ())
        if not isinstance(discovered_values, Sequence):
            raise EventError(
                "redaction_failed", "collected configuration secrets are invalid"
            )
        self._sensitive_values = _canonical_sensitive_values(
            (
                *self._sensitive_values,
                *_validated_sensitive_values(discovered_values),
            )
        )
        try:
            raw_config = config.redacted_dict()
        except (AttributeError, TypeError, ValueError):
            config_failed = True
            raw_config = {}
        else:
            config_failed = False
        if config_failed:
            raise EventError(
                "redaction_failed", "the configuration snapshot could not be redacted"
            )
        sanitized_config = _sanitize_config_document(raw_config, self._sensitive_values)
        config_json = _canonical_json(sanitized_config)

        def create_state(transaction: StateTransaction) -> None:
            transaction.execute(
                """
                INSERT INTO sessions(
                    session_id, mode, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, mode.value, "active", created_at, created_at),
            )
            transaction.execute(
                """
                INSERT INTO config_snapshots(session_id, config_json, created_at)
                VALUES (?, ?, ?)
                """,
                (session_id, config_json, created_at),
            )

        return self.append(
            EventInput(
                event_id=f"session.created:{session_id}",
                session_id=session_id,
                event_type="session.created",
                payload={"mode": mode.value, "status": "active"},
                created_at=created_at,
            ),
            create_state,
        )

    def append(
        self,
        event: EventInput,
        state_mutation: Callable[[StateTransaction], None] | None = None,
    ) -> EventRecord:
        """Append one event and optional state mutation in the same transaction."""
        clean_error: EventError | None = None
        try:
            return self._append(event, state_mutation)
        except EventError as error:
            clean_error = EventError(error.code, error.message)
        except OSError:
            clean_error = EventError(
                "filesystem_failed", "the audit filesystem operation failed"
            )
        raise clean_error

    def _append(
        self,
        event: EventInput,
        state_mutation: Callable[[StateTransaction], None] | None,
    ) -> EventRecord:
        _validate_event(event)
        payload = _sanitize_event_payload(
            event.event_type, event.payload, self._sensitive_values
        )
        payload_json = _canonical_json(payload)
        transaction_failed = False
        record: EventRecord | None = None

        with self._locked():
            anchor = self._load_or_create_anchor_locked()
            if anchor.pending is not None:
                recovery = self._recover_state_locked(anchor)
                if not recovery.ok:
                    raise EventError(
                        recovery.code,
                        "the pending audit transition is inconsistent",
                    )
                anchor = self._read_required_anchor_locked()
            verification = self._verify_state_locked(anchor)
            if not verification.ok:
                raise EventError(
                    verification.code,
                    "the audit chain is inconsistent and cannot be extended",
                )

            sequence = anchor.committed.sequence + 1
            current_hash = _event_hash(
                sequence=sequence,
                event_id=event.event_id,
                session_id=event.session_id,
                event_type=event.event_type,
                payload=payload,
                previous_hash=anchor.committed.hash,
                created_at=event.created_at,
            )
            record = EventRecord(
                sequence=sequence,
                event_id=event.event_id,
                session_id=event.session_id,
                event_type=event.event_type,
                payload=payload,
                previous_hash=anchor.committed.hash,
                current_hash=current_hash,
                created_at=event.created_at,
            )
            pending = AuditAnchor(sequence, current_hash)

            self._transition("before_pending")
            _write_anchor(
                self._store.anchor_path,
                _AnchorState(_ANCHOR_VERSION, anchor.committed, pending),
            )
            self._transition("after_pending")

            try:
                with self._store.connection() as connection:
                    connection.execute("PRAGMA defer_foreign_keys = ON")
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        INSERT INTO events(
                            sequence, event_id, session_id, event_type,
                            payload_json, previous_hash, current_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sequence,
                            event.event_id,
                            event.session_id,
                            event.event_type,
                            payload_json,
                            anchor.committed.hash,
                            current_hash,
                            event.created_at,
                        ),
                    )
                    if state_mutation is not None:
                        connection.set_authorizer(_state_mutation_authorizer)
                        try:
                            _MUTATION_CONTEXT.connection = connection
                            state_mutation(StateTransaction())
                        finally:
                            try:
                                del _MUTATION_CONTEXT.connection
                            except AttributeError:
                                pass
                            connection.set_authorizer(None)
                    if not connection.in_transaction:
                        raise sqlite3.OperationalError(
                            "the audited transaction ended inside its mutation"
                        )
                    expected_transition = _AnchorState(
                        _ANCHOR_VERSION, anchor.committed, pending
                    )
                    if self._read_required_anchor_locked() != expected_transition:
                        raise sqlite3.IntegrityError(
                            "the pending audit anchor changed inside its mutation"
                        )
                    transaction_check = _verify_exact_pending_transaction(
                        _database_rows(connection),
                        anchor.committed,
                        pending,
                        self._sensitive_values,
                    )
                    if not transaction_check.ok:
                        raise sqlite3.IntegrityError(
                            "the audit chain changed inside its mutation"
                        )
                    stored_event = connection.execute(
                        """
                        SELECT sequence, event_id, session_id, event_type,
                               payload_json, previous_hash, current_hash, created_at
                        FROM events WHERE sequence = ?
                        """,
                        (sequence,),
                    ).fetchone()
                    expected_event = (
                        sequence,
                        event.event_id,
                        event.session_id,
                        event.event_type,
                        payload_json,
                        anchor.committed.hash,
                        current_hash,
                        event.created_at,
                    )
                    if stored_event != expected_event:
                        raise sqlite3.IntegrityError(
                            "the audited event changed inside its mutation"
                        )
            except Exception:  # noqa: BLE001 - mutation is a caller boundary
                transaction_failed = True

            if transaction_failed:
                recovery_anchor = self._read_required_anchor_locked()
                self._recover_state_locked(recovery_anchor)
            else:
                self._transition("after_sqlite_commit")
                _write_anchor(
                    self._store.anchor_path,
                    _AnchorState(_ANCHOR_VERSION, pending, None),
                )
                self._transition("after_sidecar_finalization")

        if transaction_failed:
            raise EventError(
                "transaction_failed", "the audited state transaction failed"
            )
        if record is None:
            raise EventError("transaction_failed", "the audit event was not created")
        return record

    def verify(self) -> AuditVerification:
        """Verify the database chain and external anchor from genesis."""
        clean_error: EventError | None = None
        try:
            return self._verify()
        except EventError as error:
            clean_error = EventError(error.code, error.message)
        except OSError:
            clean_error = EventError(
                "filesystem_failed", "the audit filesystem operation failed"
            )
        raise clean_error

    def recent_events(self, *, limit: int = 100) -> tuple[dict[str, object], ...]:
        """Return a bounded, read-only view of the newest sanitized events."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("audit event limit must be between 1 and 200")
        try:
            with self._locked():
                rows = self._database_rows_locked()
        except EventError:
            raise
        except OSError as error:
            raise EventError(
                "filesystem_failed", "the audit database could not be read"
            ) from error
        events: list[dict[str, object]] = []
        for sequence, event_id, session_id, event_type, payload_json, _previous, _current, created_at in reversed(rows[-limit:]):
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {"error": "payload-unreadable"}
            events.append(
                {
                    "sequence": sequence,
                    "event_id": event_id,
                    "session_id": session_id,
                    "event_type": event_type,
                    "payload": payload,
                    "created_at": created_at,
                }
            )
        return tuple(events)

    def _verify(self) -> AuditVerification:
        with self._locked():
            try:
                anchor = _read_anchor(self._store.anchor_path)
            except EventError as error:
                if error.code == "anchor_invalid":
                    return AuditVerification(False, error.code, 0, _GENESIS_HASH)
                raise
            if anchor is None:
                return AuditVerification(False, "anchor_missing", 0, _GENESIS_HASH)
            return self._verify_state_locked(anchor)

    def recover_pending(self) -> AuditVerification:
        """Resolve only a valid pending transition; never infer another repair."""
        clean_error: EventError | None = None
        try:
            return self._recover_pending()
        except EventError as error:
            clean_error = EventError(error.code, error.message)
        except OSError:
            clean_error = EventError(
                "filesystem_failed", "the audit filesystem operation failed"
            )
        raise clean_error

    def _recover_pending(self) -> AuditVerification:
        with self._locked():
            try:
                anchor = _read_anchor(self._store.anchor_path)
            except EventError as error:
                if error.code == "anchor_invalid":
                    return AuditVerification(False, error.code, 0, _GENESIS_HASH)
                raise
            if anchor is None:
                return AuditVerification(False, "anchor_missing", 0, _GENESIS_HASH)
            return self._recover_state_locked(anchor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with _exclusive_audit_lock(self._store.lock_path, self._lock_timeout_s):
            yield

    def _load_or_create_anchor_locked(self) -> _AnchorState:
        anchor = _read_anchor(self._store.anchor_path)
        if anchor is not None:
            return anchor
        rows = self._database_rows_locked()
        if rows:
            raise EventError(
                "anchor_missing", "the audit anchor is missing for existing events"
            )
        genesis = _AnchorState(_ANCHOR_VERSION, AuditAnchor(0, _GENESIS_HASH), None)
        _write_anchor(self._store.anchor_path, genesis)
        return genesis

    def _read_required_anchor_locked(self) -> _AnchorState:
        anchor = _read_anchor(self._store.anchor_path)
        if anchor is None:
            raise EventError("anchor_missing", "the audit anchor is missing")
        return anchor

    def _verify_state_locked(self, anchor: _AnchorState) -> AuditVerification:
        rows = self._database_rows_locked()
        prefix, next_index = _verify_committed_prefix(
            rows, anchor.committed, self._sensitive_values
        )
        if not prefix.ok:
            return prefix
        remaining = rows[next_index:]
        if anchor.pending is None:
            if remaining:
                return AuditVerification(
                    False,
                    "tail_mismatch",
                    anchor.committed.sequence,
                    anchor.committed.hash,
                )
            return AuditVerification(
                True,
                "ok",
                anchor.committed.sequence,
                anchor.committed.hash,
            )
        pending_check = _verify_pending_row(
            remaining,
            anchor.committed,
            anchor.pending,
            self._sensitive_values,
        )
        if not pending_check.ok:
            return pending_check
        return AuditVerification(
            False,
            "pending_recovery_required",
            anchor.committed.sequence,
            anchor.committed.hash,
        )

    def _recover_state_locked(self, anchor: _AnchorState) -> AuditVerification:
        if anchor.pending is None:
            return self._verify_state_locked(anchor)
        rows = self._database_rows_locked()
        prefix, next_index = _verify_committed_prefix(
            rows, anchor.committed, self._sensitive_values
        )
        if not prefix.ok:
            return prefix
        remaining = rows[next_index:]
        pending_check = _verify_pending_row(
            remaining,
            anchor.committed,
            anchor.pending,
            self._sensitive_values,
        )
        if not pending_check.ok:
            return pending_check
        if not remaining:
            _write_anchor(
                self._store.anchor_path,
                _AnchorState(_ANCHOR_VERSION, anchor.committed, None),
            )
            return AuditVerification(
                True,
                "pending_cleared",
                anchor.committed.sequence,
                anchor.committed.hash,
            )
        _write_anchor(
            self._store.anchor_path,
            _AnchorState(_ANCHOR_VERSION, anchor.pending, None),
        )
        return AuditVerification(
            True,
            "pending_finalized",
            anchor.pending.sequence,
            anchor.pending.hash,
        )

    def _database_rows_locked(self) -> list[_DatabaseRow]:
        database_failed = False
        rows: list[tuple[Any, ...]] = []
        try:
            with self._store.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT sequence, event_id, session_id, event_type, payload_json,
                           previous_hash, current_hash, created_at
                    FROM events ORDER BY sequence
                    """
                ).fetchall()
        except (sqlite3.Error, StateError):
            database_failed = True
        if database_failed:
            raise EventError(
                "transaction_failed", "the audit database could not be read"
            )
        return _normalize_database_rows(rows)

    def _transition(self, phase: str) -> None:
        if self._transition_hook is not None:
            self._transition_hook(phase)


def _state_mutation_authorizer(
    action_code: int,
    argument_one: str | None,
    _argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action_code in {
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ANALYZE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_VTABLE,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_SAVEPOINT,
    }:
        return sqlite3.SQLITE_DENY
    if action_code in {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
    } and argument_one not in {
        "sessions",
        "config_snapshots",
        "tasks",
        "checkpoints",
        "capabilities",
    }:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _execute_state_dml(
    connection: sqlite3.Connection,
    statement: str,
    parameters: _SqlParameters,
) -> StateMutationResult:
    if (
        type(statement) is not str
        or len(statement.encode("utf-8")) > _MAX_CANONICAL_BYTES
        or re.match(r"\A\s*(?:INSERT|UPDATE|DELETE)\b", statement, re.IGNORECASE)
        is None
    ):
        raise sqlite3.ProgrammingError("only state DML is permitted")
    cursor = connection.execute(statement, parameters)
    try:
        lastrowid = cursor.lastrowid
        return StateMutationResult(
            rowcount=cursor.rowcount,
            lastrowid=lastrowid if type(lastrowid) is int else None,
        )
    finally:
        cursor.close()


def _database_rows(connection: sqlite3.Connection) -> list[_DatabaseRow]:
    rows = connection.execute(
        """
        SELECT sequence, event_id, session_id, event_type, payload_json,
               previous_hash, current_hash, created_at
        FROM events ORDER BY sequence
        """
    ).fetchall()
    return _normalize_database_rows(rows)


def _normalize_database_rows(rows: Sequence[Sequence[object]]) -> list[_DatabaseRow]:
    normalized: list[_DatabaseRow] = []
    for row in rows:
        if (
            len(row) != 8
            or type(row[0]) is not int
            or any(type(value) is not str for value in row[1:])
        ):
            raise EventError(
                "transaction_failed", "the audit database contains an invalid row"
            )
        normalized.append(cast(_DatabaseRow, tuple(row)))
    return normalized


def _verify_exact_pending_transaction(
    rows: Sequence[_DatabaseRow],
    committed: AuditAnchor,
    pending: AuditAnchor,
    sensitive_values: Sequence[str],
) -> AuditVerification:
    prefix, next_index = _verify_committed_prefix(rows, committed, sensitive_values)
    if not prefix.ok:
        return prefix
    remaining = rows[next_index:]
    if len(remaining) != 1:
        return AuditVerification(
            False, "tail_mismatch", committed.sequence, committed.hash
        )
    checked = _verify_pending_row(remaining, committed, pending, sensitive_values)
    if not checked.ok or checked.head_hash != pending.hash:
        return checked
    return checked


def _validated_lock_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 < value <= _MAX_LOCK_TIMEOUT_S
    ):
        raise EventError(
            "invalid_lock_timeout",
            "the audit lock timeout must be positive, finite, and at most 60 seconds",
        )
    return float(value)


def _validated_sensitive_values(values: Sequence[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or any(
        type(value) is not str for value in values
    ):
        raise EventError(
            "redaction_failed", "sensitive values must be a sequence of strings"
        )
    string_values = [value for value in values if isinstance(value, str)]
    return _canonical_sensitive_values(string_values)


def _canonical_sensitive_values(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {value for value in values if value}, key=lambda value: (-len(value), value)
        )
    )


def _validate_event(event: EventInput) -> None:
    if not isinstance(event, EventInput):
        raise EventError("invalid_event", "the event envelope is invalid")
    for field_name, value in (
        ("event_id", event.event_id),
        ("session_id", event.session_id),
        ("event_type", event.event_type),
        ("created_at", event.created_at),
    ):
        if (
            type(value) is not str
            or not value
            or value.strip() != value
            or "\x00" in value
            or len(value.encode("utf-8")) > _MAX_STRING_BYTES
        ):
            raise EventError("invalid_event", f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(event.created_at)
    except ValueError:
        parsed = None
    if (
        parsed is None
        or parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
    ):
        raise EventError("invalid_event", "created_at must be an aware UTC timestamp")
    if not isinstance(event.payload, Mapping):
        raise EventError("redaction_failed", "the event payload must be a mapping")


def _sanitize_event_payload(
    event_type: str,
    payload: Mapping[str, object],
    known_sensitive_values: Sequence[str],
) -> dict[str, _JsonValue]:
    collected = _collect_sensitive_values(payload)
    sensitive_values = _canonical_sensitive_values(
        (*known_sensitive_values, *collected)
    )
    if event_type == "config.snapshot":
        return _sanitize_config_document(payload, sensitive_values)
    if event_type == "session.created":
        return _sanitize_session_created(payload, sensitive_values)
    if event_type == "tool.failed":
        return _sanitize_tool_failed(payload, sensitive_values)
    if event_type == "audit.recovered":
        return _sanitize_audit_recovered(payload, sensitive_values)
    if event_type == "task.updated":
        return _sanitize_task_event(payload, sensitive_values)
    if event_type == "checkpoint.updated":
        return _sanitize_checkpoint_event(payload, sensitive_values)
    if event_type == "capability.verified":
        return _sanitize_capability_event(payload, sensitive_values)
    raise EventError("redaction_failed", "the event type has no persistence allowlist")


def _sanitize_session_created(
    payload: Mapping[str, object], sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    selected = _select_known_fields(payload, _SESSION_EVENT_FIELDS)
    if set(selected) != _SESSION_EVENT_FIELDS:
        raise EventError("redaction_failed", "the session event payload is incomplete")
    mode = _enum_text(
        selected["mode"], _EXECUTION_MODE_VALUES, sensitive_values, "session mode"
    )
    status = _enum_text(
        selected["status"], frozenset({"active"}), sensitive_values, "session status"
    )
    return {
        "mode": mode,
        "status": status,
    }


def _sanitize_tool_failed(
    payload: Mapping[str, object], sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    selected = _select_known_fields(payload, _TOOL_FAILED_EVENT_FIELDS)
    if "tool_name" not in selected or "status" not in selected:
        raise EventError("redaction_failed", "the tool failure payload is incomplete")
    result: dict[str, _JsonValue] = {
        "tool_name": _tool_name_text(
            selected["tool_name"],
            sensitive_values,
            "tool name",
            _MAX_EVENT_TEXT_BYTES,
        ),
        "status": _enum_text(
            selected["status"],
            frozenset({"error"}),
            sensitive_values,
            "tool status",
        ),
    }
    for name in ("diagnostic_code", "incident_id"):
        if name in selected:
            result[name] = _identifier_text(
                selected[name], sensitive_values, name, _MAX_IDENTIFIER_BYTES
            )
    if "duration_ms" in selected:
        duration_ms = selected["duration_ms"]
        if (
            type(duration_ms) is not int
            or duration_ms < 0
            or duration_ms > _MAX_DURATION_MS
        ):
            raise EventError("redaction_failed", "tool duration is invalid")
        result["duration_ms"] = duration_ms
    if "truncated" in selected:
        truncated = selected["truncated"]
        if type(truncated) is not bool:
            raise EventError("redaction_failed", "tool truncation flag is invalid")
        result["truncated"] = truncated
    return {name: result[name] for name in sorted(result)}


def _sanitize_audit_recovered(
    payload: Mapping[str, object], sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    selected = _select_known_fields(payload, _AUDIT_RECOVERED_EVENT_FIELDS)
    if set(selected) != _AUDIT_RECOVERED_EVENT_FIELDS:
        raise EventError("redaction_failed", "the recovery event payload is incomplete")
    recovered_sequence = selected["recovered_sequence"]
    if type(recovered_sequence) is not int or recovered_sequence < 0:
        raise EventError("redaction_failed", "the recovered sequence is invalid")
    action = _enum_text(
        selected["action"],
        _AUDIT_RECOVERY_ACTIONS,
        sensitive_values,
        "recovery action",
    )
    code = _enum_text(
        selected["code"],
        _AUDIT_RECOVERY_CODES,
        sensitive_values,
        "recovery code",
    )
    return {
        "action": action,
        "code": code,
        "recovered_sequence": recovered_sequence,
    }


def _sanitize_task_event(
    payload: Mapping[str, object], sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    selected = _select_known_fields(payload, _TASK_EVENT_FIELDS)
    if "status" not in selected or "step_index" not in selected:
        raise EventError("redaction_failed", "the task event payload is incomplete")
    result: dict[str, _JsonValue] = {
        "status": _enum_text(
            selected["status"], _TASK_STATUSES, sensitive_values, "task status"
        )
    }
    for name in ("step_index", "attempts"):
        if name in selected:
            value = selected[name]
            if type(value) is not int or value < 0:
                raise EventError("redaction_failed", f"task {name} is invalid")
            result[name] = value
    if "outcome" in selected:
        result["outcome"] = _identifier_text(
            selected["outcome"],
            sensitive_values,
            "task outcome",
            _MAX_IDENTIFIER_BYTES,
        )
    if "completion" in selected:
        completion = selected["completion"]
        if type(completion) is not bool:
            raise EventError("redaction_failed", "task completion is invalid")
        result["completion"] = completion
    return {name: result[name] for name in sorted(result)}


def _sanitize_checkpoint_event(
    payload: Mapping[str, object], sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    selected = _select_known_fields(payload, _CHECKPOINT_EVENT_FIELDS)
    if set(selected) != _CHECKPOINT_EVENT_FIELDS:
        raise EventError("redaction_failed", "checkpoint event payload is incomplete")
    return {
        "checkpoint_id": _identifier_text(
            selected["checkpoint_id"],
            sensitive_values,
            "checkpoint id",
            _MAX_IDENTIFIER_BYTES,
        ),
        "status": _enum_text(
            selected["status"],
            _CHECKPOINT_STATUSES,
            sensitive_values,
            "checkpoint status",
        ),
        "step_id": _identifier_text(
            selected["step_id"], sensitive_values, "step id", _MAX_IDENTIFIER_BYTES
        ),
    }


def _sanitize_capability_event(
    payload: Mapping[str, object], sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    selected = _select_known_fields(payload, _CAPABILITY_EVENT_FIELDS)
    if set(selected) != _CAPABILITY_EVENT_FIELDS:
        raise EventError("redaction_failed", "capability event payload is incomplete")
    available = selected["available"]
    if type(available) is not bool:
        raise EventError("redaction_failed", "capability availability is invalid")
    return {
        "available": available,
        "kind": _identifier_text(
            selected["kind"], sensitive_values, "capability kind", _MAX_IDENTIFIER_BYTES
        ),
        "name": _identifier_text(
            selected["name"], sensitive_values, "capability name", _MAX_IDENTIFIER_BYTES
        ),
        "source": _identifier_text(
            selected["source"], sensitive_values, "capability source", _MAX_IDENTIFIER_BYTES
        ),
    }


def _sanitize_config_document(
    value: Mapping[str, object], known_sensitive_values: Sequence[str]
) -> dict[str, _JsonValue]:
    if not isinstance(value, Mapping):
        raise EventError(
            "redaction_failed", "the configuration snapshot must be a mapping"
        )
    sensitive_values = _canonical_sensitive_values(
        (*known_sensitive_values, *_collect_sensitive_values(value))
    )
    selected = _select_known_fields(value, _CONFIG_TOP_LEVEL_FIELDS)
    result: dict[str, _JsonValue] = {}
    if "schema_version" in selected:
        schema_version = selected["schema_version"]
        if (
            type(schema_version) is not int
            or schema_version < 1
            or schema_version > 2**31 - 1
        ):
            raise EventError(
                "redaction_failed", "configuration schema version is invalid"
            )
        result["schema_version"] = schema_version
    if "mode" in selected:
        result["mode"] = _enum_text(
            selected["mode"],
            _EXECUTION_MODE_VALUES,
            sensitive_values,
            "configuration mode",
        )
    for name in ("free_only", "audit_required"):
        if name in selected:
            flag = selected[name]
            if type(flag) is not bool:
                raise EventError("redaction_failed", f"configuration {name} is invalid")
            result[name] = flag
    if "paths" in value:
        paths = _select_mapping_fields(value["paths"], _CONFIG_PATH_FIELDS)
        result["paths"] = {
            name: _bounded_text(
                path_value,
                sensitive_values,
                f"configuration path {name}",
                _MAX_PATH_BYTES,
            )
            for name, path_value in sorted(paths.items())
        }
    if "limits" in value:
        limits = _select_mapping_fields(value["limits"], _CONFIG_LIMIT_FIELDS)
        result["limits"] = {
            name: _config_limit(name, limit_value)
            for name, limit_value in sorted(limits.items())
        }
    if "provenance" in value:
        provenance = value["provenance"]
        if not isinstance(provenance, Mapping):
            raise EventError(
                "redaction_failed", "configuration provenance must be a mapping"
            )
        if len(provenance) > _MAX_CONTAINER_ITEMS:
            raise EventError(
                "redaction_failed", "configuration provenance is too large"
            )
        selected_provenance: dict[str, _JsonValue] = {}
        for name in sorted(_CONFIG_PROVENANCE_KEYS):
            if name in provenance:
                fields = _select_mapping_fields(
                    provenance[name], _CONFIG_PROVENANCE_FIELDS
                )
                if set(fields) != _CONFIG_PROVENANCE_FIELDS:
                    raise EventError(
                        "redaction_failed",
                        "configuration provenance is incomplete",
                    )
                field_name = _identifier_text(
                    fields["field"],
                    sensitive_values,
                    "configuration provenance field",
                    _MAX_IDENTIFIER_BYTES,
                )
                if field_name != name:
                    raise EventError(
                        "redaction_failed",
                        "configuration provenance field does not match its key",
                    )
                source = _enum_text(
                    fields["source"],
                    _CONFIG_SOURCE_VALUES,
                    sensitive_values,
                    "configuration provenance source",
                )
                source_path_value = fields["source_path"]
                source_path: str | None
                if source_path_value is None:
                    source_path = None
                else:
                    source_path = _bounded_text(
                        source_path_value,
                        sensitive_values,
                        "configuration provenance source path",
                        _MAX_PATH_BYTES,
                    )
                selected_provenance[name] = {
                    "field": field_name,
                    "source": source,
                    "source_path": source_path,
                }
        result["provenance"] = selected_provenance
    _canonical_json(result)
    return {name: result[name] for name in sorted(result)}


def _select_mapping_fields(value: object, allowed: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise EventError("redaction_failed", "an allowlisted value must be a mapping")
    if len(value) > _MAX_CONTAINER_ITEMS:
        raise EventError("redaction_failed", "an allowlisted mapping is too large")
    return {name: value[name] for name in sorted(allowed) if name in value}


def _select_known_fields(
    value: Mapping[str, object], allowed: frozenset[str]
) -> dict[str, object]:
    return {name: value[name] for name in sorted(allowed) if name in value}


def _bounded_text(
    value: object,
    sensitive_values: Sequence[str],
    field_name: str,
    maximum_bytes: int,
) -> str:
    validated = _validated_text(value, field_name, maximum_bytes)
    redacted = _redact_string(validated, sensitive_values)
    return _validated_text(redacted, field_name, maximum_bytes)


def _validated_text(
    value: object,
    field_name: str,
    maximum_bytes: int,
) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise EventError("redaction_failed", f"{field_name} is invalid")
    return value


def _structural_text(
    value: object,
    sensitive_values: Sequence[str],
    field_name: str,
    maximum_bytes: int,
) -> str:
    validated = _validated_text(value, field_name, maximum_bytes)
    if _redact_string(validated, sensitive_values) != validated:
        raise EventError(
            "redaction_failed", f"{field_name} conflicts with sensitive data"
        )
    return validated


def _identifier_text(
    value: object,
    sensitive_values: Sequence[str],
    field_name: str,
    maximum_bytes: int,
) -> str:
    validated = _structural_text(value, sensitive_values, field_name, maximum_bytes)
    if _PERSISTED_IDENTIFIER_PATTERN.fullmatch(validated) is None:
        raise EventError("redaction_failed", f"{field_name} shape is invalid")
    return validated


def _tool_name_text(
    value: object,
    sensitive_values: Sequence[str],
    field_name: str,
    maximum_bytes: int,
) -> str:
    return _identifier_text(value, sensitive_values, field_name, maximum_bytes)


def _enum_text(
    value: object,
    allowed: frozenset[str],
    sensitive_values: Sequence[str],
    field_name: str,
) -> str:
    validated = _structural_text(
        value, sensitive_values, field_name, _MAX_IDENTIFIER_BYTES
    )
    if validated not in allowed:
        raise EventError("redaction_failed", f"{field_name} is invalid")
    return validated


def _config_limit(name: str, value: object) -> int | float:
    if name in _CONFIG_INTEGER_LIMIT_FIELDS:
        if type(value) is not int or value < 0 or value > _MAX_CONFIG_INTEGER:
            raise EventError(
                "redaction_failed", f"configuration limit {name} is invalid"
            )
        return value
    if name in _CONFIG_TIMEOUT_FIELDS:
        if (
            type(value) is not float
            or not math.isfinite(value)
            or value <= 0.0
            or value > _MAX_CONFIG_TIMEOUT_S
        ):
            raise EventError(
                "redaction_failed", f"configuration limit {name} is invalid"
            )
        return value
    if name in _CONFIG_PERCENT_FIELDS:
        if (
            type(value) is not float
            or not math.isfinite(value)
            or value < 0.0
            or value > 100.0
        ):
            raise EventError(
                "redaction_failed", f"configuration limit {name} is invalid"
            )
        return value
    raise EventError("redaction_failed", "configuration limit is not allowlisted")


def _collect_sensitive_values(value: object, *, depth: int = 0) -> set[str]:
    if depth > _MAX_DEPTH:
        raise EventError("redaction_failed", "the payload nesting is too deep")
    collected: set[str] = set()
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise EventError("redaction_failed", "a payload mapping is too large")
        for key, item in value.items():
            if isinstance(key, str) and _sensitive_key(key):
                collected.update(_strings_below(item, depth=depth + 1))
            collected.update(_collect_sensitive_values(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise EventError("redaction_failed", "a payload sequence is too large")
        for item in value:
            collected.update(_collect_sensitive_values(item, depth=depth + 1))
    return {item for item in collected if item}


def _strings_below(value: object, *, depth: int) -> set[str]:
    if depth > _MAX_DEPTH:
        raise EventError("redaction_failed", "the payload nesting is too deep")
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise EventError("redaction_failed", "a payload mapping is too large")
        return {
            found
            for item in value.values()
            for found in _strings_below(item, depth=depth + 1)
        }
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise EventError("redaction_failed", "a payload sequence is too large")
        return {
            found for item in value for found in _strings_below(item, depth=depth + 1)
        }
    return set()


def _sensitive_key(name: str) -> bool:
    normalized = "".join(character.lower() for character in name if character.isalnum())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _redact_string(value: str, sensitive_values: Sequence[str]) -> str:
    result = value
    canonical_values = _canonical_sensitive_values(sensitive_values)
    for sensitive_value in canonical_values:
        result = result.replace(sensitive_value, _REDACTED)
    for safeguard in _PATTERN_SAFEGUARDS:
        result = safeguard.sub(_REDACTED, result)
    if any(sensitive_value in result for sensitive_value in canonical_values):
        raise EventError(
            "redaction_failed", "redacted text still contains sensitive data"
        )
    return result


def _canonical_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        failed = True
        encoded = b""
    else:
        failed = False
    if failed or len(encoded) > _MAX_CANONICAL_BYTES:
        raise EventError("redaction_failed", "the payload cannot be represented safely")
    return encoded.decode("utf-8")


def _event_hash(
    *,
    sequence: int,
    event_id: str,
    session_id: str,
    event_type: str,
    payload: Mapping[str, object],
    previous_hash: str,
    created_at: str,
) -> str:
    canonical = _canonical_json(
        {
            "sequence": sequence,
            "event_id": event_id,
            "session_id": session_id,
            "event_type": event_type,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": created_at,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_committed_prefix(
    rows: Sequence[_DatabaseRow],
    committed: AuditAnchor,
    sensitive_values: Sequence[str],
) -> tuple[AuditVerification, int]:
    previous_hash = _GENESIS_HASH
    next_index = 0
    for expected_sequence in range(1, committed.sequence + 1):
        if next_index >= len(rows):
            return (
                AuditVerification(
                    False,
                    "tail_mismatch",
                    expected_sequence - 1,
                    previous_hash,
                ),
                next_index,
            )
        checked = _verify_row(
            rows[next_index], expected_sequence, previous_hash, sensitive_values
        )
        if not checked.ok:
            return checked, next_index
        previous_hash = checked.head_hash
        next_index += 1
    if previous_hash != committed.hash:
        return (
            AuditVerification(
                False,
                "anchor_mismatch",
                committed.sequence,
                previous_hash,
            ),
            next_index,
        )
    return (
        AuditVerification(True, "ok", committed.sequence, committed.hash),
        next_index,
    )


def _verify_pending_row(
    remaining: Sequence[_DatabaseRow],
    committed: AuditAnchor,
    pending: AuditAnchor,
    sensitive_values: Sequence[str],
) -> AuditVerification:
    if len(remaining) > 1:
        return AuditVerification(
            False, "tail_mismatch", committed.sequence, committed.hash
        )
    if not remaining:
        return AuditVerification(True, "ok", committed.sequence, committed.hash)
    checked = _verify_row(
        remaining[0], pending.sequence, committed.hash, sensitive_values
    )
    if not checked.ok:
        return checked
    if checked.head_hash != pending.hash:
        return AuditVerification(
            False, "hash_mismatch", committed.sequence, committed.hash
        )
    return checked


def _verify_row(
    row: _DatabaseRow,
    expected_sequence: int,
    previous_hash: str,
    sensitive_values: Sequence[str],
) -> AuditVerification:
    (
        sequence,
        event_id,
        session_id,
        event_type,
        payload_json,
        stored_previous_hash,
        current_hash,
        created_at,
    ) = row
    if sequence != expected_sequence:
        return AuditVerification(
            False, "sequence_mismatch", expected_sequence - 1, previous_hash
        )
    if stored_previous_hash != previous_hash:
        return AuditVerification(
            False, "hash_mismatch", expected_sequence - 1, previous_hash
        )
    try:
        if len(payload_json.encode("utf-8")) > _MAX_CANONICAL_BYTES:
            raise EventError(
                "invalid_event", "persisted payload exceeds the byte limit"
            )
        parsed_payload = json.loads(payload_json)
        if not isinstance(parsed_payload, dict):
            raise TypeError
        sanitized_payload = _sanitize_event_payload(
            event_type, parsed_payload, sensitive_values
        )
        canonical_payload = _canonical_json(sanitized_payload)
    except (
        json.JSONDecodeError,
        UnicodeEncodeError,
        RecursionError,
        TypeError,
        EventError,
    ):
        return AuditVerification(
            False, "hash_mismatch", expected_sequence - 1, previous_hash
        )
    if parsed_payload != sanitized_payload or payload_json != canonical_payload:
        return AuditVerification(
            False, "hash_mismatch", expected_sequence - 1, previous_hash
        )
    expected_hash = _event_hash(
        sequence=sequence,
        event_id=event_id,
        session_id=session_id,
        event_type=event_type,
        payload=sanitized_payload,
        previous_hash=stored_previous_hash,
        created_at=created_at,
    )
    if current_hash != expected_hash:
        return AuditVerification(
            False, "hash_mismatch", expected_sequence - 1, previous_hash
        )
    return AuditVerification(True, "ok", sequence, current_hash)


@contextmanager
def _exclusive_audit_lock(path: Path, timeout_s: float) -> Iterator[None]:
    mutex = _mutex_for(path)
    started = time.monotonic()
    if not mutex.acquire(timeout=timeout_s):
        raise EventError("lock_timeout", "the in-process audit lock timed out")
    descriptor: int | None = None
    try:
        descriptor = _open_lock_file(path)
        remaining = max(0.0, timeout_s - (time.monotonic() - started))
        _acquire_flock(descriptor, remaining)
        try:
            yield
        finally:
            _release_flock(descriptor)
    finally:
        if descriptor is not None:
            _close_tracked_lock_descriptor(descriptor)
        mutex.release()


def _mutex_for(path: Path) -> threading.Lock:
    key = str(path)
    with _MUTEXES_GUARD:
        mutex = _MUTEXES.get(key)
        if mutex is None:
            mutex = threading.Lock()
            _MUTEXES[key] = mutex
        return mutex


def _before_fork() -> None:
    _ACTIVE_LOCK_FDS_GUARD.acquire()


def _after_fork_in_parent() -> None:
    _ACTIVE_LOCK_FDS_GUARD.release()


def _after_fork_in_child() -> None:
    global _ACTIVE_LOCK_FDS, _ACTIVE_LOCK_FDS_GUARD
    global _MUTATION_CONTEXT, _MUTEXES, _MUTEXES_GUARD
    for descriptor in tuple(_ACTIVE_LOCK_FDS):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _ACTIVE_LOCK_FDS = set()
    _ACTIVE_LOCK_FDS_GUARD = threading.Lock()
    _MUTEXES = {}
    _MUTEXES_GUARD = threading.Lock()
    _MUTATION_CONTEXT = threading.local()


def _open_lock_file(path: Path) -> int:
    _validate_lock_parent(path.parent)
    try:
        base_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    except AttributeError:
        raise EventError("lock_unsupported", "secure audit locking is unavailable")
    descriptor: int | None = None
    _ACTIVE_LOCK_FDS_GUARD.acquire()
    try:
        created = False
        try:
            descriptor = os.open(path, base_flags | os.O_CREAT | os.O_EXCL, _FILE_MODE)
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(path, base_flags)
            except OSError:
                raise EventError(
                    "unsafe_lock_file",
                    "the audit lock file could not be opened safely",
                )
        except OSError:
            raise EventError(
                "unsafe_lock_file", "the audit lock file could not be created safely"
            )
        try:
            if created:
                os.fchmod(descriptor, _FILE_MODE)
                os.fsync(descriptor)
                _fsync_directory(path.parent, "unsafe_lock_file")
            metadata = os.fstat(descriptor)
            _validate_owner_file_metadata(metadata, "unsafe_lock_file")
            _revalidate_open_path(path, metadata, "unsafe_lock_file")
        except OSError:
            _best_effort_close(descriptor)
            descriptor = None
            raise EventError(
                "unsafe_lock_file", "the audit lock file could not be validated safely"
            )
        except BaseException:
            _best_effort_close(descriptor)
            descriptor = None
            raise
        _ACTIVE_LOCK_FDS.add(descriptor)
        return descriptor
    finally:
        _ACTIVE_LOCK_FDS_GUARD.release()


def _validate_lock_parent(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise EventError("unsafe_lock_file", "the audit lock directory is inaccessible")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE
    ):
        raise EventError(
            "unsafe_lock_file", "the audit lock directory is not owner-controlled"
        )


def _acquire_flock(descriptor: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                failed = True
            else:
                failed = False
        if failed:
            raise EventError("lock_failed", "the audit lock could not be acquired")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EventError("lock_timeout", "the cross-process audit lock timed out")
        time.sleep(min(0.01, remaining))


def _release_flock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        raise EventError("lock_failed", "the audit lock could not be released")


def _close_tracked_lock_descriptor(descriptor: int) -> None:
    _ACTIVE_LOCK_FDS_GUARD.acquire()
    try:
        _ACTIVE_LOCK_FDS.discard(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            raise EventError("lock_failed", "the audit lock could not be closed")
    finally:
        _ACTIVE_LOCK_FDS_GUARD.release()


def _best_effort_close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _read_anchor(path: Path) -> _AnchorState | None:
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    except AttributeError:
        raise EventError(
            "anchor_unsupported", "secure audit anchor access is unavailable"
        )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise EventError(
            "unsafe_anchor_file", "the audit anchor could not be opened safely"
        )
    data = b""
    failure: EventError | None = None
    try:
        metadata = os.fstat(descriptor)
        _validate_owner_file_metadata(metadata, "unsafe_anchor_file")
        _revalidate_open_path(path, metadata, "unsafe_anchor_file")
        data = _read_bounded(descriptor, _MAX_ANCHOR_BYTES)
    except EventError as error:
        failure = error
    except OSError:
        failure = EventError(
            "unsafe_anchor_file", "the audit anchor could not be read safely"
        )
    try:
        os.close(descriptor)
    except OSError:
        if failure is None:
            failure = EventError(
                "unsafe_anchor_file", "the audit anchor could not be closed safely"
            )
    if failure is not None:
        raise failure
    try:
        document = json.loads(data.decode("utf-8"))
        return _parse_anchor(document)
    except (UnicodeDecodeError, json.JSONDecodeError, EventError):
        raise EventError("anchor_invalid", "the audit anchor is invalid")


def _parse_anchor(document: object) -> _AnchorState:
    if not isinstance(document, dict) or set(document) != {
        "version",
        "committed",
        "pending",
    }:
        raise EventError("anchor_invalid", "the audit anchor shape is invalid")
    if type(document["version"]) is not int or document["version"] != _ANCHOR_VERSION:
        raise EventError("anchor_invalid", "the audit anchor version is invalid")
    committed = _parse_anchor_head(document["committed"])
    pending_document = document["pending"]
    pending = None if pending_document is None else _parse_anchor_head(pending_document)
    if committed.sequence == 0:
        if committed.hash != _GENESIS_HASH:
            raise EventError("anchor_invalid", "the genesis anchor is invalid")
    elif not _is_event_hash(committed.hash):
        raise EventError("anchor_invalid", "the committed hash is invalid")
    if pending is not None and (
        pending.sequence != committed.sequence + 1 or not _is_event_hash(pending.hash)
    ):
        raise EventError("anchor_invalid", "the pending anchor is invalid")
    return _AnchorState(_ANCHOR_VERSION, committed, pending)


def _parse_anchor_head(document: object) -> AuditAnchor:
    if not isinstance(document, dict) or set(document) != {"sequence", "hash"}:
        raise EventError("anchor_invalid", "an audit head is invalid")
    sequence = document["sequence"]
    hash_value = document["hash"]
    if type(sequence) is not int or sequence < 0 or type(hash_value) is not str:
        raise EventError("anchor_invalid", "an audit head is invalid")
    return AuditAnchor(sequence, hash_value)


def _is_event_hash(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            chunk = os.read(descriptor, min(4096, limit + 1 - size))
        except OSError:
            raise EventError(
                "unsafe_anchor_file", "the audit anchor could not be read safely"
            )
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise EventError("anchor_invalid", "the audit anchor is too large")


def _write_anchor(path: Path, anchor: _AnchorState) -> None:
    document = {
        "version": anchor.version,
        "committed": {
            "sequence": anchor.committed.sequence,
            "hash": anchor.committed.hash,
        },
        "pending": (
            None
            if anchor.pending is None
            else {"sequence": anchor.pending.sequence, "hash": anchor.pending.hash}
        ),
    }
    data = _canonical_json(document).encode("utf-8")
    _validate_existing_anchor_for_replace(path)
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        temporary_path, descriptor = _create_anchor_temp(path)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent, "anchor_write_failed")
        written = _read_anchor(path)
        if written != anchor:
            raise EventError(
                "anchor_write_failed", "the audit anchor replacement was not durable"
            )
    except EventError:
        raise
    except OSError:
        raise EventError(
            "anchor_write_failed", "the audit anchor could not be replaced safely"
        )
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                if temporary_path is None:
                    raise EventError(
                        "anchor_write_failed",
                        "the audit anchor temporary file could not be closed",
                    )
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _validate_existing_anchor_for_replace(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise EventError(
            "unsafe_anchor_file", "the audit anchor could not be inspected safely"
        )
    _validate_owner_file_metadata(metadata, "unsafe_anchor_file")


def _create_anchor_temp(path: Path) -> tuple[Path, int]:
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError:
        raise EventError(
            "anchor_unsupported", "secure audit anchor replacement is unavailable"
        )
    for _attempt in range(32):
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        try:
            descriptor = os.open(candidate, flags, _FILE_MODE)
        except FileExistsError:
            continue
        except OSError:
            raise EventError(
                "anchor_write_failed", "an audit anchor temporary file was unsafe"
            )
        try:
            os.fchmod(descriptor, _FILE_MODE)
        except OSError:
            _best_effort_close(descriptor)
            try:
                candidate.unlink()
            except OSError:
                pass
            raise EventError(
                "anchor_write_failed", "an audit anchor temporary file was unsafe"
            )
        return candidate, descriptor
    raise EventError(
        "anchor_write_failed", "an audit anchor temporary file could not be created"
    )


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short audit anchor write")
        offset += written


def _fsync_directory(path: Path, code: str) -> None:
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except (AttributeError, OSError):
        raise EventError(code, "the audit directory could not be synchronized")
    try:
        os.fsync(descriptor)
    except OSError:
        raise EventError(code, "the audit directory could not be synchronized")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            raise EventError(code, "the audit directory could not be closed safely")


def _validate_owner_file_metadata(metadata: os.stat_result, code: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
    ):
        raise EventError(
            code, "audit files must be current-user regular files with mode 0600"
        )


def _revalidate_open_path(
    path: Path, opened_metadata: os.stat_result, code: str
) -> None:
    try:
        current = path.lstat()
    except OSError:
        raise EventError(code, "an audit file changed identity while opening")
    _validate_owner_file_metadata(current, code)
    if (current.st_dev, current.st_ino) != (
        opened_metadata.st_dev,
        opened_metadata.st_ino,
    ):
        raise EventError(code, "an audit file changed identity while opening")


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )
