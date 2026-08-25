"""Loopback-only browser interface for the shared ACB autonomy runtime."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol, cast
from urllib.parse import urlsplit

from autonomous_agent.core.autonomy import AutonomyRuntime, RuntimeResult
from autonomous_agent.core.config import AgentConfig
from autonomous_agent.core.goals import GoalError, GoalNormalizer
from autonomous_agent.core.learning import (
    ImprovementSuggestion,
    KnowledgeItem,
    LearningScheduler,
    LearningService,
    LearningStatus,
    SelfUpdateProposal,
    UserAccount,
)
from autonomous_agent.core.preferences import ProfileStore, UserPreferences
from autonomous_agent.core.task_state import TaskRecord
from autonomous_agent.core.user_experience import (
    AssistiveHints,
    OnboardingService,
    OnboardingStatus,
    ResponseContext,
    TaskAccessError,
    TaskFeedback,
    TaskFeedbackStore,
    VoiceCapability,
    build_response_context,
    detect_assistive_hints,
    detect_voice_capabilities,
    enforce_task_access,
)
from autonomous_agent.core.voice import SpeechResult, VoiceService, VoiceStatus

UI_NAME = "ACB – Autonome Computing Butler"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
MAX_REQUEST_BYTES = 65_536
MAX_TASK_RECORDS = 100
_REQUEST_ID_PATTERN = re.compile(r"^request-[0-9a-f]{32}$")
_SESSION_ID_PATTERN = re.compile(r"^session-[0-9a-f]{32}$")


class _TaskStore(Protocol):
    def load_task(self, session_id: str) -> TaskRecord | None: ...

    def list_tasks(
        self, *, statuses: frozenset[str] | None = None, limit: int = 100
    ) -> tuple[TaskRecord, ...]: ...


class _Runtime(Protocol):
    tasks: _TaskStore

    def run(self, raw_goal: str) -> RuntimeResult: ...

    def resume(self, session_id: str) -> RuntimeResult: ...

    def undo(self, session_id: str, step: int = 1) -> dict[str, object]: ...

    def audit_status(self) -> dict[str, object]: ...

    def audit_events(self, limit: int = 100) -> tuple[dict[str, object], ...]: ...

    def export_audit_log(self, session_id: str | None = None) -> dict[str, object]: ...


@dataclass
class UiTask:
    request_id: str
    goal: str
    status: str
    created_at: str
    session_id: str | None = None
    result: Mapping[str, object] | None = None
    error: str | None = None
    progress_percent: int = 0
    remaining_steps: int = 0
    estimated_remaining_seconds: int = 0

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "request_id": self.request_id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "progress_percent": self.progress_percent,
            "remaining_steps": self.remaining_steps,
            "estimated_remaining_seconds": self.estimated_remaining_seconds,
        }
        if self.session_id is not None:
            document["session_id"] = self.session_id
        if self.result is not None:
            document["result"] = dict(self.result)
        if self.error is not None:
            document["error"] = self.error
        return document


class RuntimeTaskController:
    """Coordinate UI tasks over one shared, persistent autonomy runtime."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        runtime: _Runtime | None = None,
        start_learning: bool = False,
        research_network: bool = False,
    ) -> None:
        self.runtime: _Runtime = (
            cast(_Runtime, AutonomyRuntime(config)) if runtime is None else runtime
        )
        self._tasks: dict[str, UiTask] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="acb-ui-runtime",
        )
        self.profile_store = ProfileStore(config.paths.state_root)
        self._research_network_requested = research_network
        self.learning = LearningService(
            config.paths.state_root,
            config.paths.project_root,
            network_enabled=(
                research_network and self.profile_store.load().allow_network_research
            ),
            interval_s=6 * 60 * 60,
        )
        self.onboarding = OnboardingService(self.profile_store)
        self.feedback = TaskFeedbackStore(config.paths.state_root)
        self.voice = VoiceService()
        self._learning_scheduler = (
            LearningScheduler(self.learning) if start_learning else None
        )
        if self._learning_scheduler is not None:
            self._learning_scheduler.start()

    def close(self) -> None:
        if self._learning_scheduler is not None:
            self._learning_scheduler.stop()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def preferences(self) -> UserPreferences:
        return self.profile_store.load()

    def update_preferences(self, changes: Mapping[str, object]) -> UserPreferences:
        normalized = dict(changes)
        current = self.profile_store.load()
        personalization_enabled = normalized.get(
            "store_personalization", current.store_personalization
        )
        if personalization_enabled is False:
            for key in (
                "nickname",
                "pronouns",
                "interests",
                "age",
                "occupation",
                "gender_identity",
            ):
                normalized[key] = None if key == "age" else ""
        preferences = self.profile_store.update(normalized)
        self.learning.network_enabled = (
            self._research_network_requested and preferences.allow_network_research
        )
        return preferences

    def onboarding_status(self) -> OnboardingStatus:
        return self.onboarding.status(account_exists=bool(self.learning.store.accounts()))

    def start_trial(self) -> OnboardingStatus:
        return self.onboarding.start_trial()

    def complete_onboarding(self) -> OnboardingStatus:
        return self.onboarding.complete()

    def response_context(self) -> ResponseContext:
        preferences = self.preferences()
        if not preferences.store_personalization:
            preferences = replace(
                preferences,
                nickname="",
                pronouns="",
                interests="",
                age=None,
                occupation="",
                gender_identity="",
            )
        return build_response_context(preferences)

    def assistive_hints(self) -> AssistiveHints:
        return detect_assistive_hints()

    def voice_capabilities(self) -> tuple[VoiceCapability, ...]:
        return detect_voice_capabilities()

    def add_feedback(self, session_id: str, rating: int, comment: str = "") -> TaskFeedback:
        if not self.preferences().store_chat_history:
            raise ValueError("chat history storage is disabled")
        feedback = self.feedback.add(session_id, rating, comment)
        self.learning.record_feedback(session_id, rating, comment)
        return feedback

    def feedback_items(self, limit: int = 50) -> tuple[TaskFeedback, ...]:
        if not self.preferences().store_chat_history:
            return ()
        return self.feedback.items(limit)

    def voice_status(self) -> VoiceStatus:
        return self.voice.status()

    def speak(self, text: str) -> SpeechResult:
        if not self.preferences().voice_output:
            return SpeechResult(False, None, "voice-output-disabled-by-preference")
        return self.voice.speak(text)

    def wake_phrase_matches(self, text: str) -> bool:
        preferences = self.preferences()
        if not preferences.voice_input or not preferences.wake_phrase_enabled:
            return False
        return self.voice.wake_phrase_matches(text, preferences.wake_phrase)

    def submit(self, goal: str) -> UiTask:
        preferences = self.preferences()
        clean_goal = (
            self.voice.remove_wake_phrase(goal, preferences.wake_phrase)
            if preferences.voice_input and preferences.wake_phrase_enabled
            else goal
        )
        normalized = GoalNormalizer().normalize(clean_goal)
        enforce_task_access(self.onboarding_status(), normalized.kind)
        request_id = f"request-{uuid.uuid4().hex}"
        task = UiTask(
            request_id=request_id,
            goal=goal,
            status="queued",
            created_at=_timestamp(),
        )
        with self._lock:
            self._tasks[request_id] = task
            self._trim_tasks()
        self._executor.submit(self._run_task, request_id, clean_goal)
        return task

    def retry(self, request_id: str) -> UiTask:
        """Requeue only a failed or rejected task using its original goal."""
        task = self.task(request_id)
        if task is None or task.status not in {"failed", "rejected"}:
            raise ValueError("only failed or rejected tasks can be retried")
        return self.submit(task.goal)

    def recover_pending(self) -> tuple[UiTask, ...]:
        """Resume tasks interrupted while pending, running, or recovering."""
        if not self.preferences().store_task_history:
            return ()
        records = self.runtime.tasks.list_tasks(
            statuses=frozenset({"pending", "running", "recovering"}),
            limit=MAX_TASK_RECORDS,
        )
        recovered: list[UiTask] = []
        for record in reversed(records):
            request_id = f"request-{uuid.uuid4().hex}"
            task = UiTask(
                request_id=request_id,
                goal=record.original_goal,
                status="recovering",
                created_at=record.created_at,
                session_id=record.session_id,
            )
            with self._lock:
                self._tasks[request_id] = task
                self._trim_tasks()
            self._executor.submit(self._resume_task, request_id, record.session_id)
            recovered.append(_copy_task(task))
        return tuple(recovered)

    def task(self, request_id: str) -> UiTask | None:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return None
            snapshot = _copy_task(task)
        self._refresh_progress(snapshot)
        return snapshot

    def tasks(self) -> list[UiTask]:
        with self._lock:
            snapshots = [_copy_task(item) for item in self._tasks.values()]
        for snapshot in snapshots:
            self._refresh_progress(snapshot)
        return snapshots

    def _refresh_progress(self, task: UiTask) -> None:
        if task.session_id is None:
            task.progress_percent = 100 if task.status in {"completed", "failed"} else 0
            return
        try:
            record = self.runtime.tasks.load_task(task.session_id)
        except Exception:  # noqa: BLE001 - progress must not break task polling
            task.progress_percent = 100 if task.status in {"completed", "failed"} else 0
            return
        if record is None or not record.plan:
            task.progress_percent = 100 if task.status in {"completed", "failed"} else 0
            return
        total = len(record.plan)
        completed_steps = min(max(record.current_step, 0), total)
        task.remaining_steps = max(total - completed_steps, 0)
        task.progress_percent = min(100, int(completed_steps * 100 / total))
        if task.status == "completed":
            task.progress_percent = 100
            task.remaining_steps = 0
        task.estimated_remaining_seconds = min(task.remaining_steps * 5, 300)

    def persisted_session(self, session_id: str) -> dict[str, object] | None:
        if not self.preferences().store_task_history:
            return None
        record = self.runtime.tasks.load_task(session_id)
        if record is None:
            return None
        return _session_document(record)

    def persisted_sessions(self, limit: int = 50) -> list[dict[str, object]]:
        if not self.preferences().store_task_history:
            return []
        records = self.runtime.tasks.list_tasks(
            limit=max(1, min(limit, MAX_TASK_RECORDS))
        )
        return [_session_document(record) for record in records]

    def undo(self, session_id: str, step: int = 1) -> dict[str, object]:
        return self.runtime.undo(session_id, step)

    def audit_status(self) -> dict[str, object]:
        return self.runtime.audit_status()

    def audit_events(self, limit: int = 100) -> list[dict[str, object]]:
        return [dict(item) for item in self.runtime.audit_events(limit)]

    def export_audit_log(self, session_id: str | None = None) -> dict[str, object]:
        return self.runtime.export_audit_log(session_id)

    def _run_task(self, request_id: str, goal: str) -> None:
        self._execute(request_id, goal, lambda: self.runtime.run(goal))

    def _resume_task(self, request_id: str, session_id: str) -> None:
        record = self.runtime.tasks.load_task(session_id)
        goal = "recovered task" if record is None else record.original_goal
        self._execute(request_id, goal, lambda: self.runtime.resume(session_id))

    def _execute(
        self, request_id: str, goal: str, operation: Callable[[], RuntimeResult]
    ) -> None:
        self._update_task(request_id, status="running")
        try:
            result = operation()
            document = result.to_dict()
            session_id = result.session_id
            try:
                document["protocol"] = self.export_audit_log(session_id)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                document["protocol"] = {"error": "protocol-export-failed"}
            if self.preferences().store_task_history:
                self.learning.review_task(goal, document)
            self._update_task(
                request_id,
                status=result.status,
                session_id=session_id,
                result=document,
            )
        except GoalError:
            self._update_task(
                request_id,
                status="rejected",
                error="goal could not be understood or was not supported",
            )
        except Exception:  # noqa: BLE001 - browser boundary must stay redacted
            self._update_task(
                request_id,
                status="failed",
                error="task execution failed before verification",
            )

    def learning_status(self) -> LearningStatus:
        return self.learning.status()

    def knowledge(self, limit: int = 50) -> tuple[KnowledgeItem, ...]:
        return self.learning.store.knowledge(limit)

    def suggestions(self, limit: int = 50) -> tuple[ImprovementSuggestion, ...]:
        return self.learning.store.suggestions(limit)

    def self_updates(self, limit: int = 50) -> tuple[SelfUpdateProposal, ...]:
        return self.learning.store.self_updates(limit)

    def research_now(self) -> dict[str, object]:
        return self.learning.research_now()

    def create_account(
        self,
        username: str,
        password: str,
        *,
        security_question: str = "",
        security_answer: str = "",
    ) -> UserAccount:
        return self.learning.create_account(
            username,
            password,
            security_question=security_question,
            security_answer=security_answer,
        )

    def authenticate(self, username: str, password: str) -> UserAccount | None:
        return self.learning.authenticate(username, password)

    def reset_password(
        self, username: str, security_answer: str, new_password: str
    ) -> UserAccount | None:
        return self.learning.reset_password(username, security_answer, new_password)

    def sync_manifest(self) -> dict[str, object]:
        return self.learning.sync_manifest()

    def learning_snapshot(self) -> dict[str, object]:
        return {
            "status": self.learning_status().to_dict(),
            "knowledge": [item.to_dict() for item in self.knowledge(50)],
            "suggestions": [item.to_dict() for item in self.suggestions(50)],
            "self_updates": [item.to_dict() for item in self.self_updates(50)],
        }

    def _update_task(
        self,
        request_id: str,
        *,
        status: str,
        session_id: str | None = None,
        result: Mapping[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return
            task.status = status
            task.session_id = session_id
            task.result = result
            task.error = error

    def _trim_tasks(self) -> None:
        while len(self._tasks) > MAX_TASK_RECORDS:
            oldest = next(iter(self._tasks))
            del self._tasks[oldest]


class AcbUiServer:
    """Serve a small browser UI while sharing one persistent runtime instance."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        host: str = DEFAULT_UI_HOST,
        port: int = DEFAULT_UI_PORT,
        runtime: _Runtime | None = None,
    ) -> None:
        self.host = validate_ui_host(host)
        self.port = _validate_port(port)
        self.token = secrets.token_urlsafe(24)
        self._controller = RuntimeTaskController(config, runtime=runtime)
        self.runtime = self._controller.runtime
        self._serving = threading.Event()
        self._httpd = _AcbHttpServer((self.host, self.port), _AcbRequestHandler, self)
        self._controller.recover_pending()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self._httpd.server_port}/"

    def serve_forever(self) -> None:
        self._serving.set()
        try:
            self._httpd.serve_forever()
        finally:
            self._serving.clear()
            self.close()

    def shutdown(self) -> None:
        if self._serving.is_set():
            self._httpd.shutdown()
        self._httpd.server_close()
        self.close()

    def close(self) -> None:
        self._controller.close()

    def submit(self, goal: str) -> UiTask:
        return self._controller.submit(goal)

    def retry(self, request_id: str) -> UiTask:
        return self._controller.retry(request_id)

    def recover_pending(self) -> tuple[UiTask, ...]:
        return self._controller.recover_pending()

    def task(self, request_id: str) -> UiTask | None:
        return self._controller.task(request_id)

    def tasks(self) -> list[UiTask]:
        return self._controller.tasks()

    def persisted_session(self, session_id: str) -> dict[str, object] | None:
        return self._controller.persisted_session(session_id)

    def persisted_sessions(self, limit: int = 50) -> list[dict[str, object]]:
        return self._controller.persisted_sessions(limit)

    def learning_snapshot(self) -> dict[str, object]:
        return self._controller.learning_snapshot()

    def authenticate(self, username: str, password: str) -> dict[str, object] | None:
        account = self._controller.authenticate(username, password)
        return None if account is None else account.to_public_dict()

    def sync_manifest(self) -> dict[str, object]:
        return self._controller.sync_manifest()

    def preferences(self) -> dict[str, object]:
        return self._controller.preferences().to_dict()

    def update_preferences(self, changes: Mapping[str, object]) -> dict[str, object]:
        return self._controller.update_preferences(changes).to_dict()

    def onboarding(self) -> dict[str, object]:
        return self._controller.onboarding_status().to_dict()

    def start_trial(self) -> dict[str, object]:
        return self._controller.start_trial().to_dict()

    def complete_onboarding(self) -> dict[str, object]:
        return self._controller.complete_onboarding().to_dict()

    def response_context(self) -> dict[str, object]:
        return self._controller.response_context().to_dict()

    def assistive_hints(self) -> dict[str, object]:
        return self._controller.assistive_hints().to_dict()

    def voice_capabilities(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self._controller.voice_capabilities()]

    def feedback(self, limit: int = 50) -> list[dict[str, object]]:
        return [item.to_dict() for item in self._controller.feedback_items(limit)]

    def undo(self, session_id: str, step: int = 1) -> dict[str, object]:
        return self._controller.undo(session_id, step)

    def audit_status(self) -> dict[str, object]:
        return self._controller.audit_status()

    def audit_events(self, limit: int = 100) -> list[dict[str, object]]:
        return self._controller.audit_events(limit)

    def export_audit_log(self, session_id: str | None = None) -> dict[str, object]:
        return self._controller.export_audit_log(session_id)

    def voice_status(self) -> dict[str, object]:
        status = self._controller.voice_status().to_dict()
        preferences = self._controller.preferences()
        status.update(
            {
                "voice_input_enabled": preferences.voice_input,
                "voice_output_enabled": preferences.voice_output,
                "wake_phrase_enabled": preferences.wake_phrase_enabled,
                "wake_phrase": preferences.wake_phrase,
                "voice_input_active": (
                    preferences.voice_input and bool(status["input_available"])
                ),
                "voice_output_active": (
                    preferences.voice_output and bool(status["output_available"])
                ),
                "wake_phrase_active": (
                    preferences.voice_input
                    and preferences.wake_phrase_enabled
                    and bool(status["input_available"])
                ),
            }
        )
        return status

    def speak(self, text: str) -> dict[str, object]:
        return self._controller.speak(text).to_dict()

    def wake_phrase_matches(self, text: str) -> dict[str, object]:
        return {"matched": self._controller.wake_phrase_matches(text)}

    def add_feedback(self, session_id: str, rating: int, comment: str = "") -> dict[str, object]:
        return self._controller.add_feedback(session_id, rating, comment).to_dict()

    def accounts(self) -> list[dict[str, object]]:
        return [account.to_public_dict() for account in self._controller.learning.store.accounts()]

    def create_account(
        self,
        username: str,
        password: str,
        security_question: str = "",
        security_answer: str = "",
    ) -> dict[str, object]:
        account = self._controller.create_account(
            username,
            password,
            security_question=security_question,
            security_answer=security_answer,
        )
        self._controller.complete_onboarding()
        return account.to_public_dict()

    def reset_password(
        self, username: str, security_answer: str, new_password: str
    ) -> dict[str, object] | None:
        account = self._controller.reset_password(username, security_answer, new_password)
        return None if account is None else account.to_public_dict()


class _AcbHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        app: AcbUiServer,
    ) -> None:
        self.app = app
        super().__init__(server_address, handler_class)


class _AcbRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        app = self._app()
        if path == "/":
            self._send_html(_render_html(app.token))
            return
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {"name": UI_NAME, "status": "ok", "runtime": "ready"},
            )
            return
        if path == "/api/tasks":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(
                HTTPStatus.OK,
                {"tasks": [task.to_dict() for task in reversed(app.tasks())]},
            )
            return
        if path == "/api/preferences":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, self._app().preferences())
            return
        if path == "/api/account":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, {"accounts": self._app().accounts()})
            return
        if path == "/api/onboarding":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, self._app().onboarding())
            return
        if path == "/api/experience":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "response_context": self._app().response_context(),
                    "assistive_hints": self._app().assistive_hints(),
                    "voice_capabilities": self._app().voice_capabilities(),
                    "voice_status": self._app().voice_status(),
                    "feedback": self._app().feedback(),
                },
            )
            return
        if path == "/api/learning":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, self._app().learning_snapshot())
            return
        if path == "/api/sync/manifest":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, self._app().sync_manifest())
            return
        if path == "/api/audit":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, self._app().audit_status())
            return
        if path == "/api/audit/events":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            try:
                events = self._app().audit_events()
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "audit events unavailable")
                return
            self._send_json(HTTPStatus.OK, {"events": events})
            return
        if path == "/api/voice":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(HTTPStatus.OK, self._app().voice_status())
            return
        if path == "/api/sessions":
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            self._send_json(
                HTTPStatus.OK, {"sessions": self._app().persisted_sessions()}
            )
            return
        if path.startswith("/api/tasks/"):
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            request_id = path.removeprefix("/api/tasks/")
            if not _REQUEST_ID_PATTERN.fullmatch(request_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, "task not found")
                return
            task = app.task(request_id)
            if task is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "task not found")
                return
            self._send_json(HTTPStatus.OK, task.to_dict())
            return
        if path.startswith("/api/sessions/"):
            if not self._authorized():
                self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
                return
            session_id = path.removeprefix("/api/sessions/")
            if not _SESSION_ID_PATTERN.fullmatch(session_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, "session not found")
                return
            session = app.persisted_session(session_id)
            if session is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "session not found")
                return
            self._send_json(HTTPStatus.OK, session)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._authorized():
            self._send_error_json(HTTPStatus.FORBIDDEN, "authorization required")
            return
        if path.startswith("/api/tasks/") and path.endswith("/retry"):
            request_id = path.removeprefix("/api/tasks/").removesuffix("/retry")
            if not _REQUEST_ID_PATTERN.fullmatch(request_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, "task not found")
                return
            try:
                task = self._app().retry(request_id)
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "task cannot be retried")
                return
            self._send_json(HTTPStatus.ACCEPTED, task.to_dict())
            return
        if path not in {
            "/api/tasks",
            "/api/preferences",
            "/api/onboarding",
            "/api/feedback",
            "/api/undo",
            "/api/voice/speak",
            "/api/voice/wake",
            "/api/account",
            "/api/audit/export",
        }:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        if path == "/api/preferences":
            try:
                changes = {key: value for key, value in payload.items() if key != "_"}
                updated = self._app().update_preferences(changes)
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "preferences are invalid")
                return
            self._send_json(HTTPStatus.OK, updated)
            return
        if path == "/api/account":
            action = payload.get("action")
            try:
                if action == "create":
                    username = payload.get("username")
                    password = payload.get("password")
                    question = payload.get("security_question", "")
                    answer = payload.get("security_answer", "")
                    if not all(isinstance(item, str) for item in (username, password, question, answer)):
                        raise ValueError("account fields are invalid")
                    username = cast(str, username)
                    password = cast(str, password)
                    question = cast(str, question)
                    answer = cast(str, answer)
                    result: Mapping[str, object] | None = self._app().create_account(
                        username, password, question, answer
                    )
                elif action == "reset-password":
                    username = payload.get("username")
                    answer = payload.get("security_answer")
                    new_password = payload.get("new_password")
                    if not all(isinstance(item, str) for item in (username, answer, new_password)):
                        raise ValueError("recovery fields are invalid")
                    username = cast(str, username)
                    answer = cast(str, answer)
                    new_password = cast(str, new_password)
                    result = self._app().reset_password(username, answer, new_password)
                    if result is None:
                        self._send_error_json(HTTPStatus.FORBIDDEN, "recovery verification failed")
                        return
                elif action == "authenticate":
                    username = payload.get("username")
                    password = payload.get("password")
                    if not all(isinstance(item, str) for item in (username, password)):
                        raise ValueError("authentication fields are invalid")
                    result = self._app().authenticate(
                        cast(str, username), cast(str, password)
                    )
                    if result is None:
                        self._send_error_json(HTTPStatus.FORBIDDEN, "authentication failed")
                        return
                else:
                    raise ValueError("account action is invalid")
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "account request is invalid")
                return
            self._send_json(HTTPStatus.OK, result or {})
            return
        if path == "/api/audit/export":
            session_id = payload.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "session id is invalid")
                return
            try:
                result = self._app().export_audit_log(session_id)
            except (TypeError, ValueError, RuntimeError, OSError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "audit export failed")
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if path == "/api/onboarding":
            action = payload.get("action")
            try:
                status = (
                    self._app().start_trial()
                    if action == "start-trial"
                    else self._app().complete_onboarding()
                    if action == "complete"
                    else None
                )
            except (TypeError, ValueError, RuntimeError):
                status = None
            if status is None:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "onboarding action is invalid")
                return
            self._send_json(HTTPStatus.OK, status)
            return
        if path == "/api/feedback":
            session_id = payload.get("session_id")
            rating = payload.get("rating")
            comment = payload.get("comment", "")
            try:
                if not isinstance(session_id, str) or type(rating) is not int or not isinstance(comment, str):
                    raise ValueError("feedback shape is invalid")
                feedback = self._app().add_feedback(session_id, rating, comment)
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "feedback is invalid")
                return
            self._send_json(HTTPStatus.OK, feedback)
            return
        if path == "/api/undo":
            session_id = payload.get("session_id")
            step = payload.get("step", 1)
            try:
                if not isinstance(session_id, str) or type(step) is not int:
                    raise ValueError("undo shape is invalid")
                result = self._app().undo(session_id, step)
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "undo is invalid or unavailable")
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if path == "/api/voice/speak":
            text = payload.get("text")
            try:
                if not isinstance(text, str):
                    raise TypeError("speech text is invalid")
                result = self._app().speak(text)
            except (TypeError, ValueError, RuntimeError):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "speech request is invalid")
                return
            self._send_json(HTTPStatus.OK, result)
            return
        if path == "/api/voice/wake":
            text = payload.get("text")
            if not isinstance(text, str):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "wake phrase text is invalid")
                return
            self._send_json(HTTPStatus.OK, self._app().wake_phrase_matches(text))
            return
        goal = payload.get("goal")
        if type(goal) is not str or not goal.strip():
            self._send_error_json(HTTPStatus.BAD_REQUEST, "goal is required")
            return
        try:
            task = self._app().submit(goal)
        except TaskAccessError as error:
            self._send_error_json(HTTPStatus.FORBIDDEN, str(error))
            return
        except GoalError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "goal is invalid or unsupported")
            return
        except Exception:  # noqa: BLE001 - do not disclose local paths
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "task rejected")
            return
        self._send_json(HTTPStatus.ACCEPTED, task.to_dict())

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _app(self) -> AcbUiServer:
        server = cast(_AcbHttpServer, self.server)
        return server.app

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-ACB-Token", "")
        return secrets.compare_digest(supplied, self._app().token)

    def _read_json(self) -> dict[str, object] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type != "application/json":
            self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON required")
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "request length required")
            return None
        if length > MAX_REQUEST_BYTES:
            self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid request")
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if type(value) is not dict:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "JSON object required")
            return None
        return cast(dict[str, object], value)

    def _send_json(self, status: HTTPStatus, value: Mapping[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._headers("text/html; charset=utf-8", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def _headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )


def validate_ui_host(host: str) -> str:
    """Allow only loopback listeners; never expose the task executor remotely."""
    if type(host) is not str or not host or host == "localhost":
        return DEFAULT_UI_HOST if host == "localhost" else _invalid_host()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("UI host must be loopback") from error
    if not address.is_loopback:
        raise ValueError("UI host must be loopback")
    return host


def _invalid_host() -> str:
    raise ValueError("UI host must be loopback")


def _validate_port(port: int) -> int:
    if type(port) is not int or not 0 <= port <= 65_535:
        raise ValueError("UI port is invalid")
    return port


def _copy_task(task: UiTask) -> UiTask:
    return UiTask(
        request_id=task.request_id,
        goal=task.goal,
        status=task.status,
        created_at=task.created_at,
        session_id=task.session_id,
        result=task.result,
        error=task.error,
        progress_percent=task.progress_percent,
        remaining_steps=task.remaining_steps,
        estimated_remaining_seconds=task.estimated_remaining_seconds,
    )


def _session_document(record: TaskRecord) -> dict[str, object]:
    document: dict[str, object] = {
        "session_id": record.session_id,
        "original_goal": record.original_goal,
        "status": record.status,
        "current_step": record.current_step,
        "attempts": record.attempts,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if record.failure_fingerprint is not None:
        document["failure_fingerprint"] = record.failure_fingerprint
    if record.completion is not None:
        document["completion"] = dict(record.completion)
    return document


def _timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _render_html(token: str) -> str:
    safe_token = json.dumps(token, ensure_ascii=True)
    return _HTML.replace("__ACB_TOKEN__", safe_token)


_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ACB – Autonome Computing Butler</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1220; --panel:#121c2e; --line:#263653; --ink:#e7edf8; --muted:#9fb0ca; --accent:#5eead4; --warn:#fbbf24; --bad:#fb7185; }
    * { box-sizing: border-box; } body { margin:0; min-height:100vh; font:16px/1.5 system-ui,sans-serif; color:var(--ink); background:radial-gradient(circle at top right,#183b55 0,var(--bg) 42%); }
    main { width:min(1120px,calc(100% - 32px)); margin:0 auto; padding:44px 0 64px; } header { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:28px; }
    h1 { margin:0; font-size:clamp(2rem,5vw,3.5rem); letter-spacing:-.04em; } h1 span { color:var(--accent); } p { color:var(--muted); }
    .badge { border:1px solid #2c766d; color:var(--accent); padding:7px 11px; border-radius:999px; white-space:nowrap; font-size:.85rem; }
    .panel { background:color-mix(in srgb,var(--panel) 92%,transparent); border:1px solid var(--line); border-radius:18px; padding:22px; box-shadow:0 18px 60px #02061766; }
    textarea { width:100%; min-height:118px; resize:vertical; border:1px solid #385070; border-radius:12px; padding:14px; color:var(--ink); background:#09111f; font:inherit; }
    button { margin-top:14px; border:0; border-radius:10px; padding:11px 18px; color:#052e2b; background:var(--accent); font-weight:700; cursor:pointer; } button:disabled { opacity:.5; cursor:wait; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:18px; margin-top:18px; } .label { color:var(--muted); font-size:.82rem; text-transform:uppercase; letter-spacing:.08em; }
    #state { margin-top:10px; white-space:pre-wrap; color:var(--muted); } #history { display:grid; gap:10px; margin-top:12px; } .task { border-left:3px solid var(--accent); padding:10px 12px; background:#0b1728; border-radius:7px; } .task.failed,.task.rejected { border-color:var(--bad); } .task.completed { border-color:var(--accent); }
    code { color:#c4d4ef; } .error { color:var(--bad); } .success { color:var(--accent); } pre { white-space:pre-wrap; overflow:auto; color:#cbd5e1; }
  </style>
</head>
<body>
<main>
  <header><div><div class="label">Lokaler Autonomie-Runtime</div><h1><span>ACB</span> Butler</h1><p>Natürliche Aufträge ausführen, prüfen und dauerhaft nachvollziehbar machen.</p></div><div class="badge" id="health">Runtime wird geprüft …</div></header>
  <section class="panel"><div class="label">Neuer Auftrag</div><form id="task-form"><textarea id="goal" required placeholder="Zum Beispiel: Erstelle die Datei notes.txt mit dem Inhalt Hallo ACB"></textarea><button id="submit" type="submit">Auftrag ausführen</button></form><div id="state">Bereit. Die Oberfläche ist ausschließlich auf diesem Rechner erreichbar.</div></section>
  <div class="grid"><section class="panel"><div class="label">Aktueller Status</div><div id="current">Noch kein Auftrag gestartet.</div></section><section class="panel"><div class="label">Verlauf</div><div id="history">Keine Aufträge in dieser Sitzung.</div></section><section class="panel"><div class="label">Wissen &amp; Verbesserungen</div><div id="learning">Lernstatus wird geladen …</div></section></div>
</main>
<script>
const TOKEN = __ACB_TOKEN__; const form = document.getElementById('task-form'); const goal = document.getElementById('goal'); const submit = document.getElementById('submit'); const state = document.getElementById('state'); const current = document.getElementById('current'); const history = document.getElementById('history'); const learning = document.getElementById('learning');
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
async function request(path, options={}) { const response = await fetch(path, {...options, headers:{'Content-Type':'application/json','X-ACB-Token':TOKEN,...(options.headers||{})}}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Anfrage fehlgeschlagen'); return data; }
function renderTask(task) { const result = task.result || {}; const completion = result.completion || {}; const criteria = (completion.criteria || []).map(item => `<div>${item.passed ? '✓' : '✗'} ${esc(item.criterion_id)}: ${esc(item.evidence)}</div>`).join(''); current.innerHTML = `<strong class="${task.status === 'completed' ? 'success' : task.status === 'failed' || task.status === 'rejected' ? 'error' : ''}">${esc(task.status)}</strong><p>${esc(task.goal)}</p><div>Fortschritt: ${esc(task.progress_percent)}% · verbleibende Schritte: ${esc(task.remaining_steps)} · geschätzt: ${esc(task.estimated_remaining_seconds)}s</div>${task.session_id ? `<div>Session: <code>${esc(task.session_id)}</code></div>` : ''}${criteria ? `<p>${criteria}</p>` : ''}${task.error ? `<p class="error">${esc(task.error)}</p>` : ''}${result.outputs ? `<details><summary>Ausgabe</summary><pre>${esc(JSON.stringify(result.outputs,null,2))}</pre></details>` : ''}`; }
async function refresh() { try { const data = await request('/api/tasks',{headers:{}}); const sessions = await request('/api/sessions',{headers:{}}); const items = data.tasks.length ? data.tasks : sessions.sessions; history.innerHTML = items.length ? items.map(task => `<div class="task ${esc(task.status)}"><strong>${esc(task.status)}</strong> · ${esc(task.goal || task.original_goal)}${task.session_id ? `<br><code>${esc(task.session_id)}</code>` : ''}</div>`).join('') : 'Keine gespeicherten Aufträge.'; } catch (error) { state.textContent = error.message; state.className='error'; } }
async function refreshLearning() { try { const data = await request('/api/learning',{headers:{}}); const status = data.status || {}; const suggestions = data.suggestions || []; const updates = data.self_updates || []; learning.innerHTML = `<div>Wissen: ${esc(status.knowledge_count || 0)} · Vorschläge: ${esc(status.suggestion_count || 0)} · Self-Updates: ${esc(status.self_update_count || 0)}</div>${suggestions.slice(0,3).map(item => `<p><strong>${esc(item.title)}</strong><br>${esc(item.description)}</p>`).join('')}${updates.slice(0,2).map(item => `<p><strong>Self-Update:</strong> ${esc(item.title)}</p>`).join('')}`; } catch (error) { learning.textContent = 'Lernstatus nicht verfügbar.'; } }
async function poll(id) { try { const task = await request(`/api/tasks/${encodeURIComponent(id)}`,{headers:{}}); renderTask(task); await refresh(); if (['queued','running'].includes(task.status)) setTimeout(() => poll(id), 600); else { submit.disabled=false; state.textContent = task.status === 'completed' ? 'Auftrag vollständig verifiziert.' : 'Auftrag beendet; bitte Evidenz prüfen.'; state.className = task.status === 'completed' ? 'success' : 'error'; } } catch (error) { submit.disabled=false; state.textContent=error.message; state.className='error'; } }
form.addEventListener('submit', async event => { event.preventDefault(); submit.disabled=true; state.textContent='Auftrag angenommen …'; state.className=''; try { const task=await request('/api/tasks',{method:'POST',body:JSON.stringify({goal:goal.value})}); renderTask(task); goal.value=''; poll(task.request_id); } catch(error) { submit.disabled=false; state.textContent=error.message; state.className='error'; } });
request('/api/health',{headers:{}}).then(data => { document.getElementById('health').textContent = data.status === 'ok' ? '● Runtime bereit' : 'Runtime prüfen'; }).catch(() => { document.getElementById('health').textContent='Runtime nicht erreichbar'; }); refresh(); refreshLearning();
</script>
</body>
</html>"""


__all__ = [
    "DEFAULT_UI_HOST",
    "DEFAULT_UI_PORT",
    "MAX_REQUEST_BYTES",
    "UI_NAME",
    "AcbUiServer",
    "RuntimeTaskController",
    "validate_ui_host",
]
