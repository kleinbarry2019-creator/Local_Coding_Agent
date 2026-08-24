"""Audited persistent task, checkpoint, and capability state."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from autonomous_agent.core.events import AuditLog, EventInput, StateTransaction
from autonomous_agent.core.state import CoreStateStore

_TASK_STATUSES = frozenset(
    {"pending", "running", "recovering", "blocked", "failed", "completed"}
)
_ALLOWED_TRANSITIONS = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"running", "recovering", "failed", "completed"}),
    "recovering": frozenset({"running", "recovering", "failed"}),
    "failed": frozenset({"running", "recovering", "failed"}),
    "blocked": frozenset({"running", "failed"}),
    "completed": frozenset({"completed"}),
}


@dataclass(frozen=True)
class TaskRecord:
    session_id: str
    original_goal: str
    normalized_goal: Mapping[str, object]
    plan: tuple[Mapping[str, object], ...]
    status: str
    current_step: int
    attempts: int
    failure_fingerprint: str | None
    completion: Mapping[str, object] | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_id: str
    session_id: str
    step_id: str
    manifest: Mapping[str, object]
    status: str
    created_at: str


class TaskStateStore:
    """Persist runtime state only through the core DB and audit transaction."""

    def __init__(self, store: CoreStateStore, audit: AuditLog) -> None:
        self.store = store
        self.audit = audit

    def create_task(
        self,
        session_id: str,
        original_goal: str,
        normalized_goal: Mapping[str, object],
        plan: tuple[Mapping[str, object], ...],
        *,
        created_at: str | None = None,
    ) -> TaskRecord:
        now = _timestamp() if created_at is None else created_at
        normalized_json = _canonical_json(dict(normalized_goal))
        plan_json = _canonical_json([dict(step) for step in plan])

        def mutation(transaction: StateTransaction) -> None:
            transaction.execute(
                """INSERT INTO tasks(
                    session_id, original_goal, normalized_goal_json, plan_json,
                    status, current_step, attempts, failure_fingerprint,
                    completion_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    original_goal,
                    normalized_json,
                    plan_json,
                    "pending",
                    0,
                    0,
                    None,
                    None,
                    now,
                    now,
                ),
            )

        self.audit.append(
            EventInput(
                event_id=_event_id("task-created"),
                session_id=session_id,
                event_type="task.updated",
                payload={
                    "status": "pending",
                    "step_index": 0,
                    "attempts": 0,
                    "completion": False,
                },
                created_at=now,
            ),
            mutation,
        )
        record = self.load_task(session_id)
        if record is None:
            raise RuntimeError("task state was not persisted")
        return record

    def transition(
        self,
        session_id: str,
        *,
        status: str,
        current_step: int,
        attempts: int,
        failure_fingerprint: str | None = None,
        completion: Mapping[str, object] | None = None,
        outcome: str = "state-updated",
    ) -> TaskRecord:
        if status not in _TASK_STATUSES:
            raise ValueError("task status is invalid")
        if current_step < 0 or attempts < 0:
            raise ValueError("task counters are invalid")
        current = self.load_task(session_id)
        if current is None:
            raise RuntimeError("task state does not exist")
        if status not in _ALLOWED_TRANSITIONS[current.status]:
            raise ValueError("task status transition is invalid")
        now = _timestamp()
        completion_json = (
            None if completion is None else _canonical_json(dict(completion))
        )

        def mutation(transaction: StateTransaction) -> None:
            result = transaction.execute(
                """UPDATE tasks SET
                    status = ?, current_step = ?, attempts = ?,
                    failure_fingerprint = ?, completion_json = ?, updated_at = ?
                    WHERE session_id = ? AND status = ?""",
                (
                    status,
                    current_step,
                    attempts,
                    failure_fingerprint,
                    completion_json,
                    now,
                    session_id,
                    current.status,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("task state does not exist")

        self.audit.append(
            EventInput(
                event_id=_event_id("task-transition"),
                session_id=session_id,
                event_type="task.updated",
                payload={
                    "status": status,
                    "step_index": current_step,
                    "attempts": attempts,
                    "outcome": outcome,
                    "completion": status == "completed",
                },
                created_at=now,
            ),
            mutation,
        )
        record = self.load_task(session_id)
        if record is None:
            raise RuntimeError("task state disappeared")
        return record

    def load_task(self, session_id: str) -> TaskRecord | None:
        with self.store.connection() as connection:
            row = connection.execute(
                """SELECT session_id, original_goal, normalized_goal_json,
                          plan_json, status, current_step, attempts,
                          failure_fingerprint, completion_json, created_at, updated_at
                   FROM tasks WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        normalized = json.loads(str(row[2]))
        raw_plan = json.loads(str(row[3]))
        completion = None if row[8] is None else json.loads(str(row[8]))
        if not isinstance(normalized, dict) or not isinstance(raw_plan, list):
            raise TypeError("persisted task state is invalid")
        if completion is not None and not isinstance(completion, dict):
            raise TypeError("persisted task completion is invalid")
        return TaskRecord(
            session_id=str(row[0]),
            original_goal=str(row[1]),
            normalized_goal=normalized,
            plan=tuple(item for item in raw_plan if isinstance(item, dict)),
            status=str(row[4]),
            current_step=int(row[5]),
            attempts=int(row[6]),
            failure_fingerprint=None if row[7] is None else str(row[7]),
            completion=completion,
            created_at=str(row[9]),
            updated_at=str(row[10]),
        )

    def list_tasks(
        self,
        *,
        statuses: frozenset[str] | None = None,
        limit: int = 100,
    ) -> tuple[TaskRecord, ...]:
        """Return recent persisted tasks for restart recovery and local UIs."""
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("task list limit is invalid")
        selected = None if statuses is None else frozenset(statuses)
        if selected is not None and not selected.issubset(_TASK_STATUSES):
            raise ValueError("task list status is invalid")
        query = """SELECT session_id FROM tasks"""
        parameters: list[object] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            query += f" WHERE status IN ({placeholders})"
            parameters.extend(sorted(selected))
        query += " ORDER BY updated_at DESC LIMIT ?"
        parameters.append(limit)
        with self.store.connection() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        records = tuple(
            record
            for row in rows
            if (record := self.load_task(str(row[0]))) is not None
        )
        return records

    def create_checkpoint(
        self,
        session_id: str,
        step_id: str,
        manifest: Mapping[str, object],
        *,
        checkpoint_id: str | None = None,
    ) -> CheckpointRecord:
        checkpoint_id = (
            f"checkpoint-{uuid.uuid4().hex}"
            if checkpoint_id is None
            else checkpoint_id
        )
        now = _timestamp()
        manifest_json = _canonical_json(dict(manifest))

        def mutation(transaction: StateTransaction) -> None:
            transaction.execute(
                """INSERT INTO checkpoints(
                    checkpoint_id, session_id, step_id, manifest_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (checkpoint_id, session_id, step_id, manifest_json, "created", now),
            )

        self.audit.append(
            EventInput(
                event_id=_event_id("checkpoint-created"),
                session_id=session_id,
                event_type="checkpoint.updated",
                payload={
                    "checkpoint_id": checkpoint_id,
                    "step_id": step_id,
                    "status": "created",
                },
                created_at=now,
            ),
            mutation,
        )
        return CheckpointRecord(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            step_id=step_id,
            manifest=dict(manifest),
            status="created",
            created_at=now,
        )

    def mark_checkpoint(self, checkpoint_id: str, session_id: str, status: str) -> None:
        if status not in {"restored", "discarded"}:
            raise ValueError("checkpoint status is invalid")
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT step_id FROM checkpoints WHERE checkpoint_id = ? AND session_id = ?",
                (checkpoint_id, session_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("checkpoint does not exist")
        now = _timestamp()

        def mutation(transaction: StateTransaction) -> None:
            result = transaction.execute(
                "UPDATE checkpoints SET status = ? WHERE checkpoint_id = ?",
                (status, checkpoint_id),
            )
            if result.rowcount != 1:
                raise RuntimeError("checkpoint update failed")

        self.audit.append(
            EventInput(
                event_id=_event_id("checkpoint-transition"),
                session_id=session_id,
                event_type="checkpoint.updated",
                payload={
                    "checkpoint_id": checkpoint_id,
                    "step_id": str(row[0]),
                    "status": status,
                },
                created_at=now,
            ),
            mutation,
        )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _event_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


__all__ = ["CheckpointRecord", "TaskRecord", "TaskStateStore"]
