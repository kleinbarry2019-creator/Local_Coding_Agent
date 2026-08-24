"""Bounded local voice adapters for ACB.

The adapter never records silently and never downloads a model. Speech output
is an explicit, short-lived child process; speech input reports capability
only until a user explicitly starts an installed offline recognizer.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - fixed local voice executable only
import unicodedata
from dataclasses import dataclass

MAX_SPEECH_BYTES = 8_192
VOICE_TIMEOUT_S = 15


@dataclass(frozen=True)
class VoiceStatus:
    output_engine: str | None
    output_available: bool
    input_engine: str | None
    input_available: bool
    network_used: bool
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "output_engine": self.output_engine,
            "output_available": self.output_available,
            "input_engine": self.input_engine,
            "input_available": self.input_available,
            "network_used": self.network_used,
            "message": self.message,
        }


@dataclass(frozen=True)
class SpeechResult:
    started: bool
    engine: str | None
    diagnostic: str

    def to_dict(self) -> dict[str, object]:
        return {
            "started": self.started,
            "engine": self.engine,
            "diagnostic": self.diagnostic,
        }


class VoiceService:
    """Use only already-installed local TTS/STT components."""

    def __init__(self) -> None:
        self.output = _first_available(("espeak-ng", "spd-say", "piper"))
        self.input = _first_available(("whisper", "faster-whisper", "vosk-transcriber"))

    def status(self) -> VoiceStatus:
        return VoiceStatus(
            output_engine=self.output,
            output_available=self.output is not None,
            input_engine=self.input,
            input_available=self.input is not None,
            network_used=False,
            message=(
                "Lokale Sprachausgabe verfügbar; Spracheingabe wird nur nach "
                "explizitem Start eines installierten Offline-Erkenners aktiviert."
                if self.output is not None
                else "Kein lokaler Sprachausgabe-Adapter gefunden; ACB bleibt textbasiert."
            ),
        )

    def speak(self, text: str) -> SpeechResult:
        if type(text) is not str or not text.strip():
            raise ValueError("speech text is invalid")
        if len(text.encode("utf-8")) > MAX_SPEECH_BYTES:
            raise ValueError("speech text is too large")
        if self.output is None:
            return SpeechResult(False, None, "output-engine-unavailable")
        executable = self.output
        command = [executable, text]
        if executable == "piper":
            return SpeechResult(False, executable, "piper-requires-an-explicit-local-model")
        try:
            completed = subprocess.run(  # nosec B603 - fixed executable, no shell
                command,
                check=False,
                shell=False,
                capture_output=True,
                timeout=VOICE_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return SpeechResult(False, executable, "output-process-failed")
        return SpeechResult(
            completed.returncode == 0,
            executable,
            "spoken" if completed.returncode == 0 else "output-process-failed",
        )

    @staticmethod
    def wake_phrase_matches(text: str, phrase: str) -> bool:
        """Match a configured local wake phrase without recording or networking."""
        if type(text) is not str or type(phrase) is not str:
            return False
        normalized_text = _normalize_phrase(text)
        normalized_phrase = _normalize_phrase(phrase)
        return bool(normalized_phrase) and (
            normalized_text == normalized_phrase
            or normalized_text.startswith(f"{normalized_phrase} ")
        )


def _first_available(names: tuple[str, ...]) -> str | None:
    for name in names:
        if shutil.which(name) is not None:
            return name
    return None


def _normalize_phrase(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(char for char in normalized if char.isalnum() or char.isspace()).split())


__all__ = ["SpeechResult", "VoiceService", "VoiceStatus"]
