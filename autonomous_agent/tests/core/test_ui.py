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
from autonomous_agent.core.learning import KnowledgeItem
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


def _get_json(
    url: str, *, token: str | None = None
) -> tuple[int, Mapping[str, object], dict[str, str]]:
    request = Request(url)
    if token is not None:
        request.add_header("X-ACB-Token", token)
    with urlopen(request, timeout=2) as response:
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


def test_controller_honors_disabled_voice_output_preference(tmp_path: Path) -> None:
    controller = RuntimeTaskController(
        _config(tmp_path), runtime=cast(_Runtime, _FakeRuntime())
    )
    try:
        controller.update_preferences({"voice_output": False})
        result = controller.speak("Hallo ACB")
        assert result.started is False
        assert result.diagnostic == "voice-output-disabled-by-preference"
    finally:
        controller.close()


def test_controller_honors_network_research_preference(tmp_path: Path) -> None:
    controller = RuntimeTaskController(_config(tmp_path), research_network=True)
    try:
        assert controller.learning_status().network_enabled is True
        preferences = controller.update_preferences({"allow_network_research": False})
        assert preferences.allow_network_research is False
        assert controller.learning_status().network_enabled is False
        result = controller.research_now()
        assert result["status"] == "offline"
    finally:
        controller.close()


def test_browser_ui_research_endpoint_runs_without_opening_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = AcbUiServer(_config(tmp_path), port=0, research_network=True)
    monkeypatch.setattr(
        server._controller,
        "research_now",
        lambda: {"status": "ok", "items": 2, "sources": [{"name": "test"}]},
    )
    thread = _start(server)
    try:
        status, result = _post_json(
            f"{server.url}api/learning/research",
            {},
            token=server.token,
        )
        assert status == 200
        assert result["status"] == "ok"
        assert result["items"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_controller_attaches_relevant_learning_context_to_task(tmp_path: Path) -> None:
    controller = RuntimeTaskController(
        _config(tmp_path), runtime=cast(_Runtime, _FakeRuntime())
    )
    controller.learning.store.add_knowledge(
        KnowledgeItem(
            item_id="knowledge-context",
            title="List files security guidance",
            url="https://arxiv.org/abs/1234.7777",
            source="test",
            topic="security",
            summary="safe list files workflow",
            published_at=None,
            discovered_at="2026-08-25T00:00:00+00:00",
            trust="allow-listed-feed",
        )
    )
    try:
        task = controller.submit("list files")
        assert task.to_dict()["learning_context"]
        for _ in range(50):
            current = controller.task(task.request_id)
            assert current is not None
            if current.status == "completed":
                break
            time.sleep(0.01)
        assert current is not None
        assert current.result is not None
        assert "learning_context" in current.result
    finally:
        controller.close()


def test_controller_response_context_uses_feedback_hint(tmp_path: Path) -> None:
    controller = RuntimeTaskController(
        _config(tmp_path), runtime=cast(_Runtime, _FakeRuntime())
    )
    try:
        controller.add_feedback("session-local", 4, "Bitte einfacher erklären.")
        assert controller.response_context().feedback_hint == (
            "einfacher und schrittweise formulieren"
        )
    finally:
        controller.close()


def test_controller_can_disable_adaptive_response_learning(tmp_path: Path) -> None:
    controller = RuntimeTaskController(
        _config(tmp_path), runtime=cast(_Runtime, _FakeRuntime())
    )
    try:
        controller.add_feedback("session-local", 4, "Bitte einfacher erklären.")
        controller.update_preferences({"adaptive_response_learning": False})
        assert controller.response_context().feedback_hint == ""
        # Feedback remains available for the local audit/history setting; only
        # automatic response-style adaptation is disabled.
        assert controller.feedback_items()
    finally:
        controller.close()


def test_controller_can_reset_active_response_profile(tmp_path: Path) -> None:
    controller = RuntimeTaskController(
        _config(tmp_path), runtime=cast(_Runtime, _FakeRuntime())
    )
    try:
        controller.add_feedback("session-local", 4, "Bitte einfacher erklären.")
        assert controller.response_context().feedback_hint
        profile = controller.reset_response_profile()
        assert profile["preferred_hint"] is None
        assert controller.response_context().feedback_hint == ""
    finally:
        controller.close()


def test_controller_honors_history_and_personalization_privacy(tmp_path: Path) -> None:
    controller = RuntimeTaskController(_config(tmp_path))
    try:
        controller.update_preferences(
            {
                "nickname": "Privat",
                "interests": "intern",
                "store_personalization": False,
                "store_task_history": False,
                "store_chat_history": False,
            }
        )
        context = controller.response_context()
        assert context.nickname == ""
        assert context.audience_age is None
        stored = controller.preferences()
        assert stored.nickname == ""
        assert stored.interests == ""
        assert controller.persisted_sessions() == []
        assert controller.persisted_session("session-0123456789abcdef0123456789abcdef") is None
        assert controller.recover_pending() == ()
        assert controller.feedback_items() == ()
        with pytest.raises(ValueError):
            controller.add_feedback(
                "session-0123456789abcdef0123456789abcdef", 10, "privat"
            )
        controller.update_preferences({"nickname": "erneut privat"})
        assert controller.preferences().nickname == ""
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
        with urlopen(server.url, timeout=2) as response:
            html = response.read().decode("utf-8")
        assert 'id="preferences-form"' in html
        assert 'id="save-preferences"' in html
        assert "allow_network_research" in html
        assert 'id="account-form"' in html
        assert 'id="account-create"' in html

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
            {
                "theme": "light",
                "simple_language": True,
                "nickname": "Alex",
                "interests": "Robotics",
                "age": 35,
                "occupation": "Developer",
                "gender_identity": "nonbinary",
                "pronoun_mode": "custom",
                "pronouns": "they/them",
                "store_personalization": True,
                "voice_input": True,
                "wake_phrase_enabled": True,
            },
            token=server.token,
        )
        assert status == 200
        assert preferences["theme"] == "light"
        assert preferences["simple_language"] is True
        assert server.preferences()["nickname"] == "Alex"
        assert server.preferences()["gender_identity"] == "nonbinary"
        status, updated_preferences = _post_json(
            f"{server.url}api/preferences",
            {
                "theme": "dark",
                "response_style": "detailed",
                "knowledge_level": "developer",
                "gendered_language": True,
                "allow_network_research": False,
            },
            token=server.token,
        )
        assert status == 200
        assert updated_preferences["theme"] == "dark"
        assert updated_preferences["response_style"] == "detailed"
        assert updated_preferences["knowledge_level"] == "developer"
        assert updated_preferences["allow_network_research"] is False
        voice_status, voice, _ = _get_json(
            f"{server.url}api/voice", token=server.token
        )
        assert voice_status == 200
        assert voice["voice_input_enabled"] is True
        assert voice["voice_output_enabled"] is False
        assert voice["wake_phrase_enabled"] is True
        assert voice["voice_input_active"] is bool(voice["input_available"])
        assert voice["voice_output_active"] is False
        assert voice["wake_phrase_active"] is bool(voice["input_available"])
        assert isinstance(voice["wake_phrase"], str)
        status, experience, _ = _get_json(
            f"{server.url}api/experience", token=server.token
        )
        assert status == 200
        assert experience["voice_status"] == voice
        status, learning, _ = _get_json(
            f"{server.url}api/learning", token=server.token
        )
        assert status == 200
        assert {"status", "knowledge", "suggestions", "self_updates"} <= set(
            learning
        )
        status, manifest, _ = _get_json(
            f"{server.url}api/sync/manifest", token=server.token
        )
        assert status == 200
        assert manifest["sync_scope"] == "local-first; explicit future pairing required"
        assert "password_hash" not in str(manifest)
        status, wake = _post_json(
            f"{server.url}api/voice/wake",
            {"text": "Hey ACB, starte"},
            token=server.token,
        )
        assert status == 200
        assert wake == {"matched": True}
        status, _ = _post_json(
            f"{server.url}api/preferences",
            {"voice_input": False},
            token=server.token,
        )
        assert status == 200
        status, wake = _post_json(
            f"{server.url}api/voice/wake",
            {"text": "Hey ACB, starte"},
            token=server.token,
        )
        assert status == 200
        assert wake == {"matched": False}
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
            status, task, _ = _get_json(
                f"{server.url}api/tasks/{request_id}", token=server.token
            )
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


def test_browser_ui_account_create_and_authenticate(tmp_path: Path) -> None:
    server = AcbUiServer(_config(tmp_path), port=0)
    thread = _start(server)
    try:
        status, created = _post_json(
            f"{server.url}api/account",
            {
                "action": "create",
                "username": "browser-user",
                "password": "correct-horse-battery",
                "security_question": "Lieblingsfarbe?",
                "security_answer": "blau",
            },
            token=server.token,
        )
        assert status == 200
        assert created["username"] == "browser-user"
        assert "password_hash" not in str(created)
        status, accounts, _ = _get_json(
            f"{server.url}api/account", token=server.token
        )
        assert status == 200
        assert any(item["username"] == "browser-user" for item in accounts["accounts"])
        status, authenticated = _post_json(
            f"{server.url}api/account",
            {
                "action": "authenticate",
                "username": "browser-user",
                "password": "correct-horse-battery",
            },
            token=server.token,
        )
        assert status == 200
        assert authenticated["username"] == "browser-user"
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
            _, task, _ = _get_json(
                f"{server.url}api/tasks/{request_id}", token=server.token
            )
            if task["status"] in {"completed", "failed", "rejected"}:
                break
            time.sleep(0.02)
        assert task["status"] == "completed"
        result = cast(Mapping[str, object], task["result"])
        completion = cast(Mapping[str, object], result["completion"])
        assert completion["completed"] is True
        protocol = cast(Mapping[str, object], result["protocol"])
        assert protocol["event_count"] > 0
        assert (tmp_path / "project" / str(protocol["path"])).is_file()
        assert (tmp_path / "project" / "ui-result.txt").read_text() == "verified"
        session_id = task["session_id"]
        status, sessions, _ = _get_json(
            f"{server.url}api/sessions", token=server.token
        )
        assert status == 200
        assert any(
            item["session_id"] == session_id for item in sessions["sessions"]
        )
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
        onboarding_status, onboarding, _ = _get_json(
            f"{server.url}api/onboarding", token=server.token
        )
        assert onboarding_status == 200
        assert onboarding["phase"] == "ready"
        status, authenticated = _post_json(
            f"{server.url}api/account",
            {
                "action": "authenticate",
                "username": "owner",
                "password": "correct horse battery",
            },
            token=server.token,
        )
        assert status == 200
        assert authenticated["username"] == "owner"
        assert "password_hash" not in str(authenticated)
        status, _ = _post_json(
            f"{server.url}api/account",
            {"action": "authenticate", "username": "owner", "password": "wrong"},
            token=server.token,
        )
        assert status == 403
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


def test_ui_learning_context_endpoint_is_bounded(tmp_path: Path) -> None:
    server = AcbUiServer(_config(tmp_path), port=0)
    thread = _start(server)
    try:
        status, result = _post_json(
            f"{server.url}api/learning/context",
            {"goal": "Verbessere die lokale Sicherheit", "limit": 3},
            token=server.token,
        )
        assert status == 200
        assert result["context"] == []
        status, _ = _post_json(
            f"{server.url}api/learning/context",
            {"goal": "x", "limit": -1},
            token=server.token,
        )
        assert status == 400
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_ui_capability_matrix_reports_limits_and_gates(tmp_path: Path) -> None:
    server = AcbUiServer(_config(tmp_path), port=0)
    thread = _start(server)
    try:
        status, matrix, _ = _get_json(
            f"{server.url}api/capabilities", token=server.token
        )
        assert status == 200
        assert matrix["schema_version"] == 1
        entries = {item["id"]: item for item in matrix["capabilities"]}
        assert entries["coding.autonomous"]["status"] == "available"
        assert entries["learning.self-update"]["status"] == "gated"
        assert entries["security.remote-control"]["limits"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_ui_plugin_catalog_is_authenticated_and_metadata_only(tmp_path: Path) -> None:
    server = AcbUiServer(_config(tmp_path), port=0)
    thread = _start(server)
    try:
        status, payload, _ = _get_json(f"{server.url}api/plugins", token=server.token)
        assert status == 200
        assert payload["plugins"] == []
        assert payload["execution"] == "policy-gated"
        with pytest.raises(HTTPError) as error:
            _get_json(f"{server.url}api/plugins")
        assert error.value.code == 403
    finally:
        server.shutdown()
        thread.join(timeout=2)
