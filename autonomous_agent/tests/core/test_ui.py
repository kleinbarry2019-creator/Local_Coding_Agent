from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from autonomous_agent import cli
from autonomous_agent.core.autonomy import (
    CompletionReport,
    CriterionResult,
    RuntimeResult,
)
from autonomous_agent.core.config import (
    AgentConfig,
    ConfigSource,
    ExecutionMode,
    FieldProvenance,
    ResolvedPaths,
    ResourceLimits,
)
from autonomous_agent.core.task_state import TaskRecord
from autonomous_agent.ui import (
    UI_NAME,
    AcbUiServer,
    RuntimeTaskController,
    _Runtime,
    validate_ui_host,
)


def _config(tmp_path: Path) -> AgentConfig:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return AgentConfig(
        schema_version=1,
        mode=ExecutionMode.AUTONOMOUS,
        paths=ResolvedPaths(
            config_file=tmp_path / "config.toml",
            project_file=project / ".local-agent.toml",
            project_root=project,
            state_root=tmp_path / "state",
        ),
        limits=ResourceLimits(),
        free_only=True,
        audit_required=True,
        provenance={
            "mode": FieldProvenance("mode", ConfigSource.CLI, None),
        },
    )


def _start(server: AcbUiServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            with urlopen(f"{server.url}api/health", timeout=1) as response:
                assert response.status == 200
                return thread
        except OSError:
            time.sleep(0.01)
    raise AssertionError("UI server did not start")


def _get_json(url: str) -> tuple[int, Mapping[str, object], dict[str, str]]:
    with urlopen(url, timeout=2) as response:
        return response.status, json.loads(response.read()), dict(response.headers)


def _post_json(
    url: str,
    payload: object,
    *,
    token: str | None,
    content_type: str = "application/json",
) -> tuple[int, Mapping[str, object]]:
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["X-ACB-Token"] = token
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@dataclass
class _FakeTasks:
    def load_task(self, session_id: str) -> TaskRecord | None:
        del session_id
        raise AssertionError("fake task store is only used for task execution")

    def list_tasks(
        self, *, statuses: frozenset[str] | None = None, limit: int = 100
    ) -> tuple[TaskRecord, ...]:
        del statuses, limit
        return ()


class _FakeRuntime:
    def __init__(self) -> None:
        self.tasks = _FakeTasks()

    def run(self, goal: str) -> RuntimeResult:
        return RuntimeResult(
            session_id="session-0123456789abcdef0123456789abcdef",
            status="completed",
            completion=CompletionReport(
                completed=True,
                original_goal_matched=True,
                executed=True,
                tested=True,
                e2e_verified=True,
                criteria=(CriterionResult("e2e", True, "public-boundary-reverified"),),
            ),
            outputs=({"goal": goal, "success": True},),
        )


class _RecoveryTasks:
    def __init__(self, record: TaskRecord) -> None:
        self.record = record

    def load_task(self, session_id: str) -> TaskRecord | None:
        return self.record if session_id == self.record.session_id else None

    def list_tasks(
        self, *, statuses: frozenset[str] | None = None, limit: int = 100
    ) -> tuple[TaskRecord, ...]:
        del limit
        return (
            (self.record,)
            if statuses is None or self.record.status in statuses
            else ()
        )


class _RecoveryRuntime:
    def __init__(self, record: TaskRecord) -> None:
        self.tasks = _RecoveryTasks(record)

    def run(self, goal: str) -> RuntimeResult:
        del goal
        raise AssertionError("recovery runtime should resume, not start")

    def resume(self, session_id: str) -> RuntimeResult:
        return _FakeRuntime().run(f"resumed {session_id}")


def test_ui_is_loopback_only() -> None:
    assert validate_ui_host("localhost") == "127.0.0.1"
    assert validate_ui_host("127.0.0.1") == "127.0.0.1"
    with pytest.raises(ValueError):
        validate_ui_host("0.0.0.0")
    with pytest.raises(ValueError):
        validate_ui_host("example.invalid")


def test_cli_exposes_loopback_ui_command() -> None:
    parsed = cli.build_parser().parse_args(
        ["ui", "--host", "127.0.0.1", "--port", "0", "--open"]
    )
    assert vars(parsed) == {
        "command": "ui",
        "project": None,
        "state_dir": None,
        "host": "127.0.0.1",
        "port": 0,
        "open": True,
    }
    with pytest.raises(cli._CliArgumentError):
        cli.build_parser().parse_args(["ui", "--host", "0.0.0.0"])

    app = cli.build_parser().parse_args(["app"])
    assert vars(app) == {
        "command": "app",
        "project": None,
        "state_dir": None,
    }
    learn = cli.build_parser().parse_args(["learn", "--network", "--json"])
    assert vars(learn) == {
        "command": "learn",
        "network": True,
        "json": True,
        "project": None,
        "state_dir": None,
    }


def test_controller_resumes_persisted_interrupted_work(tmp_path: Path) -> None:
    record = TaskRecord(
        session_id="session-0123456789abcdef0123456789abcdef",
        original_goal="list files",
        normalized_goal={"kind": "list-files"},
        plan=({"step_id": "step-list"},),
        status="running",
        current_step=0,
        attempts=1,
        failure_fingerprint=None,
        completion=None,
        created_at="2026-08-24T00:00:00+00:00",
        updated_at="2026-08-24T00:00:01+00:00",
    )
    runtime = _RecoveryRuntime(record)
    controller = RuntimeTaskController(
        _config(tmp_path), runtime=cast(_Runtime, runtime)
    )
    try:
        recovered = controller.recover_pending()
        assert len(recovered) == 1
        for _ in range(50):
            task = controller.task(recovered[0].request_id)
            assert task is not None
            if task.status == "completed":
                break
            time.sleep(0.01)
        assert task.status == "completed"
        assert task.session_id == record.session_id
    finally:
        controller.close()


def test_ui_http_boundary_requires_token_and_serves_security_headers(
    tmp_path: Path,
) -> None:
    server = AcbUiServer(
        _config(tmp_path), port=0, runtime=cast(_Runtime, _FakeRuntime())
    )
    thread = _start(server)
    try:
        status, health, headers = _get_json(f"{server.url}api/health")
        assert status == 200
        assert health == {"name": UI_NAME, "status": "ok", "runtime": "ready"}
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in headers["Content-Security-Policy"]

        status, denied = _post_json(
            f"{server.url}api/tasks", {"goal": "list files"}, token=None
        )
        assert status == 403
        assert denied == {"error": "authorization required"}

        status, rejected = _post_json(
            f"{server.url}api/tasks",
            {"goal": "list files"},
            token=server.token,
            content_type="text/plain",
        )
        assert status == 415
        assert rejected == {"error": "JSON required"}

        status, accepted = _post_json(
            f"{server.url}api/tasks", {"goal": "list files"}, token=server.token
        )
        assert status == 202
        status, preferences = _post_json(
            f"{server.url}api/preferences",
            {"theme": "light", "simple_language": True, "nickname": "Alex"},
            token=server.token,
        )
        assert status == 200
        assert preferences["theme"] == "light"
        assert preferences["simple_language"] is True
        assert server.preferences()["nickname"] == "Alex"
        status, onboarding = _post_json(
            f"{server.url}api/onboarding", {"action": "start-trial"}, token=server.token
        )
        assert status == 200
        assert onboarding["phase"] == "trial"
        status, denied_trial = _post_json(
            f"{server.url}api/tasks", {"goal": "run echo restricted"}, token=server.token
        )
        assert status == 403
        assert "Testphase" in str(denied_trial["error"])
        request_id = accepted["request_id"]
        assert isinstance(request_id, str)
        for _ in range(50):
            status, task, _ = _get_json(f"{server.url}api/tasks/{request_id}")
            assert status == 200
            if task["status"] == "completed":
                break
            time.sleep(0.01)
        assert task["status"] == "completed"
        result = cast(Mapping[str, object], task["result"])
        completion = cast(Mapping[str, object], result["completion"])
        assert completion["e2e_verified"] is True
        status, feedback = _post_json(
            f"{server.url}api/feedback",
            {"session_id": task["session_id"], "rating": 9, "comment": "gut"},
            token=server.token,
        )
        assert status == 200
        assert feedback["rating"] == 9
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_ui_real_http_e2e_executes_and_persists_task(tmp_path: Path) -> None:
    server = AcbUiServer(_config(tmp_path), port=0)
    thread = _start(server)
    try:
        status, accepted = _post_json(
            f"{server.url}api/tasks",
            {"goal": "Erstelle `ui-result.txt` mit dem Inhalt `verified`"},
            token=server.token,
        )
        assert status == 202
        request_id = accepted["request_id"]
        task: Mapping[str, object]
        for _ in range(100):
            _, task, _ = _get_json(f"{server.url}api/tasks/{request_id}")
            if task["status"] in {"completed", "failed", "rejected"}:
                break
            time.sleep(0.02)
        assert task["status"] == "completed"
        result = cast(Mapping[str, object], task["result"])
        completion = cast(Mapping[str, object], result["completion"])
        assert completion["completed"] is True
        assert (tmp_path / "project" / "ui-result.txt").read_text() == "verified"
        session_id = task["session_id"]
        status, undone = _post_json(
            f"{server.url}api/undo",
            {"session_id": session_id, "step": 1},
            token=server.token,
        )
        assert status == 200
        assert undone["restored"] is True
        assert undone["audit_ok"] is True
        assert not (tmp_path / "project" / "ui-result.txt").exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert isinstance(session_id, str)
    # The result is still represented by the persisted core session after the
    # HTTP process has stopped; a restart can inspect it through the same store.
    restarted = AcbUiServer(_config(tmp_path), port=0)
    try:
        persisted = restarted.persisted_session(session_id)
        assert persisted is not None
        assert persisted["status"] == "completed"
        completion = cast(Mapping[str, object], persisted["completion"])
        assert completion["completed"] is True
    finally:
        restarted.shutdown()


def test_ui_http_account_recovery_never_returns_credentials(tmp_path: Path) -> None:
    server = AcbUiServer(_config(tmp_path), port=0)
    thread = _start(server)
    try:
        status, created = _post_json(
            f"{server.url}api/account",
            {
                "action": "create",
                "username": "owner",
                "password": "correct horse battery",
                "security_question": "Lieblingsfarbe?",
                "security_answer": "Blau",
            },
            token=server.token,
        )
        assert status == 200
        assert created["recovery_configured"] is True
        assert "password_hash" not in str(created)
        status, denied = _post_json(
            f"{server.url}api/account",
            {
                "action": "reset-password",
                "username": "owner",
                "security_answer": "falsch",
                "new_password": "new correct password",
            },
            token=server.token,
        )
        assert status == 403
        assert "recovery" in str(denied["error"])
        status, reset = _post_json(
            f"{server.url}api/account",
            {
                "action": "reset-password",
                "username": "owner",
                "security_answer": "blau",
                "new_password": "new correct password",
            },
            token=server.token,
        )
        assert status == 200
        assert reset["username"] == "owner"
        assert "password_hash" not in str(reset)
    finally:
        server.shutdown()
        thread.join(timeout=2)
