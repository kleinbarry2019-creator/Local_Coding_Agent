"""Local profile and presentation preferences for the ACB desktop client.

The profile is deliberately local-first.  It contains presentation and
accessibility preferences, not credentials or remote account secrets.  Values
are validated at the storage boundary so the UI, chat adapter, and future
voice adapter all consume the same contract.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

MAX_PROFILE_BYTES = 128_000
THEMES = frozenset({"system", "light", "dark"})
KNOWLEDGE_LEVELS = frozenset(
    {"beginner", "hobbyist", "advanced", "expert", "developer"}
)
RESPONSE_STYLES = frozenset({"concise", "balanced", "detailed"})
PRONOUN_MODES = frozenset({"neutral", "gendered", "custom"})
MAX_TEXT = 160
MAX_WAKE_PHRASE = 80


@dataclass(frozen=True)
class UserPreferences:
    """Validated preferences used by the response and desktop layers."""

    schema_version: int = 1
    theme: str = "system"
    language: str = "de"
    response_style: str = "balanced"
    simple_language: bool = False
    gendered_language: bool = False
    pronoun_mode: str = "neutral"
    pronouns: str = ""
    nickname: str = ""
    interests: str = ""
    age: int | None = None
    occupation: str = ""
    gender_identity: str = ""
    knowledge_level: str = "hobbyist"
    voice_input: bool = False
    voice_output: bool = False
    wake_phrase_enabled: bool = False
    wake_phrase: str = "Hey ACB"
    input_device: str = "system-default"
    output_device: str = "system-default"
    input_volume: int = 80
    output_volume: int = 80
    reduced_motion: bool = False
    high_contrast: bool = False
    large_text: bool = False
    screen_reader: bool = False
    braille_input: bool = False
    motor_assistance: bool = False
    cognitive_support: bool = False
    color_blind_mode: str = "none"
    store_chat_history: bool = True
    store_task_history: bool = True
    store_personalization: bool = True
    allow_network_research: bool = True
    require_confirmation_for_sensitive_data: bool = True
    onboarding_complete: bool = False
    test_mode_until: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


class ProfileStore:
    """Atomic owner-controlled profile storage below the validated state root."""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root.resolve(strict=False) / "profile"
        self.path = self.root / "preferences.json"
        self._lock = threading.RLock()
        self._ensure_directory()

    def load(self) -> UserPreferences:
        with self._lock:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                )
            except FileNotFoundError:
                return UserPreferences()
            except OSError as error:
                raise RuntimeError("profile could not be opened safely") from error
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                    raise RuntimeError("profile permissions are unsafe")
                if metadata.st_size > MAX_PROFILE_BYTES:
                    raise RuntimeError("profile is too large")
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = -1
                    value = json.load(stream)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeError("profile is invalid") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            return _from_mapping(value)

    def save(self, preferences: UserPreferences) -> UserPreferences:
        validated = _from_mapping(preferences.to_dict())
        encoded = json.dumps(
            validated.to_dict(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_PROFILE_BYTES:
            raise ValueError("profile is too large")
        with self._lock:
            descriptor, temporary = tempfile.mkstemp(prefix=".preferences.", dir=self.root)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(encoded)
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
        return validated

    def update(self, changes: Mapping[str, object]) -> UserPreferences:
        current = self.load().to_dict()
        unknown = set(changes) - {field.name for field in fields(UserPreferences)}
        if unknown:
            raise ValueError("unknown preference: " + min(unknown))
        current.update(changes)
        return self.save(_from_mapping(current))

    def _ensure_directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)


def _from_mapping(value: object) -> UserPreferences:
    if not isinstance(value, Mapping):
        raise TypeError("profile must be an object")
    defaults = UserPreferences().to_dict()
    data = {key: value.get(key, default) for key, default in defaults.items()}
    data["schema_version"] = 1
    for name in (
        "theme",
        "response_style",
        "pronoun_mode",
        "knowledge_level",
        "color_blind_mode",
    ):
        if not isinstance(data[name], str):
            raise TypeError(f"invalid preference: {name}")
    if data["theme"] not in THEMES:
        raise ValueError("invalid preference: theme")
    if data["response_style"] not in RESPONSE_STYLES:
        raise ValueError("invalid preference: response_style")
    if data["pronoun_mode"] not in PRONOUN_MODES:
        raise ValueError("invalid preference: pronoun_mode")
    if data["knowledge_level"] not in KNOWLEDGE_LEVELS:
        raise ValueError("invalid preference: knowledge_level")
    if data["color_blind_mode"] not in {"none", "red-green", "blue-yellow", "monochrome"}:
        raise ValueError("invalid preference: color_blind_mode")
    for name in (
        "simple_language",
        "gendered_language",
        "voice_input",
        "voice_output",
        "wake_phrase_enabled",
        "reduced_motion",
        "high_contrast",
        "large_text",
        "screen_reader",
        "braille_input",
        "motor_assistance",
        "cognitive_support",
        "store_chat_history",
        "store_task_history",
        "store_personalization",
        "allow_network_research",
        "require_confirmation_for_sensitive_data",
        "onboarding_complete",
    ):
        if not isinstance(data[name], bool):
            raise TypeError(f"invalid preference: {name}")
    for name in (
        "language",
        "pronouns",
        "nickname",
        "interests",
        "occupation",
        "gender_identity",
        "wake_phrase",
        "input_device",
        "output_device",
    ):
        if not isinstance(data[name], str) or len(data[name]) > MAX_TEXT:
            raise ValueError(f"invalid preference: {name}")
    if len(data["wake_phrase"]) > MAX_WAKE_PHRASE:
        raise ValueError("invalid preference: wake_phrase")
    if data["age"] is not None and (
        not isinstance(data["age"], int) or isinstance(data["age"], bool) or not 5 <= data["age"] <= 120
    ):
        raise ValueError("invalid preference: age")
    for name in ("input_volume", "output_volume"):
        if not isinstance(data[name], int) or isinstance(data[name], bool) or not 0 <= data[name] <= 100:
            raise ValueError(f"invalid preference: {name}")
    if data["test_mode_until"] is not None and not isinstance(data["test_mode_until"], str):
        raise ValueError("invalid preference: test_mode_until")
    return UserPreferences(**data)


__all__ = [
    "KNOWLEDGE_LEVELS",
    "PRONOUN_MODES",
    "RESPONSE_STYLES",
    "THEMES",
    "ProfileStore",
    "UserPreferences",
]
