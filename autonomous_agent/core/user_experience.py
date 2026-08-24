"""First-run, response-context, assistive-device, and task-feedback contracts.

These primitives keep user experience state separate from task execution. They
are local-only and bounded: device probing never installs drivers, voice
probing never opens a microphone, and feedback never contains task secrets.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autonomous_agent.core.preferences import ProfileStore, UserPreferences

MAX_FEEDBACK = 200
MAX_COMMENT = 2_000
MAX_FILE_BYTES = 512_000


@dataclass(frozen=True)
class OnboardingStatus:
    phase: str
    trial_active: bool
    trial_until: str | None
    account_required: bool
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "trial_active": self.trial_active,
            "trial_until": self.trial_until,
            "account_required": self.account_required,
            "message": self.message,
        }


@dataclass(frozen=True)
class ResponseContext:
    style: str
    knowledge_level: str
    simple_language: bool
    gendered_language: bool
    nickname: str
    pronouns: str
    audience_age: int | None
    next_steps: str

    def to_dict(self) -> dict[str, object]:
        return {
            "style": self.style,
            "knowledge_level": self.knowledge_level,
            "simple_language": self.simple_language,
            "gendered_language": self.gendered_language,
            "nickname": self.nickname,
            "pronouns": self.pronouns,
            "audience_age": self.audience_age,
            "next_steps": self.next_steps,
        }


@dataclass(frozen=True)
class AssistiveHints:
    platform: str
    screen_reader: bool
    braille: bool
    large_text: bool
    high_contrast: bool
    motor_assistance: bool
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "screen_reader": self.screen_reader,
            "braille": self.braille,
            "large_text": self.large_text,
            "high_contrast": self.high_contrast,
            "motor_assistance": self.motor_assistance,
            "source": self.source,
        }


@dataclass(frozen=True)
class VoiceCapability:
    name: str
    direction: str
    available: bool
    executable: str | None
    offline: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "direction": self.direction,
            "available": self.available,
            "executable": self.executable,
            "offline": self.offline,
        }


@dataclass(frozen=True)
class TaskFeedback:
    feedback_id: str
    session_id: str
    rating: int
    comment: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "feedback_id": self.feedback_id,
            "session_id": self.session_id,
            "rating": self.rating,
            "comment": self.comment,
            "created_at": self.created_at,
        }


class OnboardingService:
    """Manage first-run, limited trial, and completed local profile states."""

    def __init__(self, store: ProfileStore) -> None:
        self.store = store

    def status(self, *, account_exists: bool = False) -> OnboardingStatus:
        preferences = self.store.load()
        if preferences.onboarding_complete:
            return OnboardingStatus("ready", False, None, False, "ACB ist eingerichtet.")
        trial_until = preferences.test_mode_until
        if trial_until is not None:
            try:
                active = datetime.fromisoformat(trial_until) > datetime.now(UTC)
            except ValueError:
                active = False
            if active:
                return OnboardingStatus(
                    "trial",
                    True,
                    trial_until,
                    False,
                    "Testphase aktiv; sensible Systemaktionen bleiben geschützt.",
                )
        if account_exists:
            return OnboardingStatus(
                "account-required",
                False,
                trial_until,
                True,
                "Die Testphase ist abgelaufen. Bitte richte das lokale Konto ein.",
            )
        return OnboardingStatus(
            "welcome", False, None, False, "Willkommen bei ACB. Profil optional einrichten."
        )

    def start_trial(self, *, days: int = 3) -> OnboardingStatus:
        if not 1 <= days <= 30:
            raise ValueError("trial duration is invalid")
        until = (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")
        self.store.update({"test_mode_until": until})
        return self.status()

    def complete(self) -> OnboardingStatus:
        self.store.update({"onboarding_complete": True, "test_mode_until": None})
        return self.status()


def build_response_context(preferences: UserPreferences) -> ResponseContext:
    if preferences.simple_language:
        next_steps = "kurz, nummeriert und mit genau einer nächsten Aktion"
    elif preferences.knowledge_level == "beginner":
        next_steps = "mit einer kurzen Erklärung vor jedem Schritt"
    elif preferences.knowledge_level in {"expert", "developer"}:
        next_steps = "kompakt, technisch präzise und mit prüfbaren Befehlen"
    else:
        next_steps = "verständlich, mit einer kurzen Begründung und klaren nächsten Schritten"
    return ResponseContext(
        style=preferences.response_style,
        knowledge_level=preferences.knowledge_level,
        simple_language=preferences.simple_language,
        gendered_language=preferences.gendered_language,
        nickname=preferences.nickname,
        pronouns=preferences.pronouns,
        audience_age=preferences.age,
        next_steps=next_steps,
    )


def detect_assistive_hints(environ: Mapping[str, str] | None = None) -> AssistiveHints:
    """Read non-invasive OS hints; never change settings or install drivers."""
    values = os.environ if environ is None else environ
    screen_reader = bool(values.get("ORCA_HOST", "")) or shutil.which("orca") is not None
    braille = bool(values.get("BRLTTY_BAUD", "")) or shutil.which("brltty") is not None
    large_text = values.get("GDK_SCALE", "1") not in {"", "1"}
    high_contrast = values.get("GTK_THEME", "").lower().startswith("highcontrast")
    motor = bool(values.get("ACB_MOTOR_ASSISTANCE", ""))
    return AssistiveHints(
        platform=platform.system().lower(),
        screen_reader=screen_reader,
        braille=braille,
        large_text=large_text,
        high_contrast=high_contrast,
        motor_assistance=motor,
        source="local-hints-only",
    )


def detect_voice_capabilities() -> tuple[VoiceCapability, ...]:
    candidates = (
        ("whisper", "input", ("whisper", "faster-whisper"), True),
        ("vosk", "input", ("vosk-transcriber",), True),
        ("piper", "output", ("piper",), True),
        ("espeak-ng", "output", ("espeak-ng",), True),
        ("speech-dispatcher", "output", ("spd-say",), True),
    )
    capabilities: list[VoiceCapability] = []
    for name, direction, executables, offline in candidates:
        executable = next((shutil.which(item) for item in executables if shutil.which(item)), None)
        capabilities.append(VoiceCapability(name, direction, executable is not None, executable, offline))
    return tuple(capabilities)


class TaskFeedbackStore:
    """Bounded local ratings that do not duplicate the audited task state."""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root.resolve(strict=False) / "feedback"
        self.path = self.root / "tasks.json"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def items(self, limit: int = 50) -> tuple[TaskFeedback, ...]:
        raw = self._read()
        result = [_feedback(item) for item in raw if isinstance(item, Mapping)]
        return tuple(result[: max(1, min(limit, MAX_FEEDBACK))])

    def add(self, session_id: str, rating: int, comment: str = "") -> TaskFeedback:
        if not isinstance(session_id, str) or not session_id.startswith("session-"):
            raise ValueError("session id is invalid")
        if type(rating) is not int or not 1 <= rating <= 10:
            raise ValueError("rating must be between 1 and 10")
        if type(comment) is not str or len(comment) > MAX_COMMENT:
            raise ValueError("comment is invalid")
        feedback = TaskFeedback(
            f"feedback-{uuid.uuid4().hex}",
            session_id,
            rating,
            comment,
            datetime.now(UTC).isoformat(timespec="seconds"),
        )
        with self._lock:
            values = [feedback.to_dict(), *self._read()][:MAX_FEEDBACK]
            self._write(values)
        return feedback

    def _read(self) -> list[object]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        except OSError as error:
            raise RuntimeError("feedback could not be opened safely") from error
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or metadata.st_size > MAX_FILE_BYTES:
                raise RuntimeError("feedback permissions or size are unsafe")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                value = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("feedback is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return value if isinstance(value, list) else []

    def _write(self, values: list[object]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".tasks.", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(json.dumps(values, ensure_ascii=False, separators=(",", ":")))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = ""
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def _feedback(value: Mapping[str, object]) -> TaskFeedback:
    session_id = value.get("session_id")
    rating = value.get("rating")
    comment = value.get("comment", "")
    feedback_id = value.get("feedback_id")
    created_at = value.get("created_at")
    if not all(isinstance(item, str) for item in (session_id, comment, feedback_id, created_at)):
        raise RuntimeError("feedback record is invalid")
    if type(rating) is not int or not 1 <= rating <= 10:
        raise RuntimeError("feedback rating is invalid")
    assert isinstance(session_id, str)
    assert isinstance(comment, str)
    assert isinstance(feedback_id, str)
    assert isinstance(created_at, str)
    return TaskFeedback(feedback_id, session_id, rating, comment, created_at)


__all__ = [
    "AssistiveHints",
    "OnboardingService",
    "OnboardingStatus",
    "ResponseContext",
    "TaskFeedback",
    "TaskFeedbackStore",
    "VoiceCapability",
    "build_response_context",
    "detect_assistive_hints",
    "detect_voice_capabilities",
]
