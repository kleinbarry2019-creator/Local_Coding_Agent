from __future__ import annotations

import os
from pathlib import Path

import pytest

from autonomous_agent.core.preferences import ProfileStore, UserPreferences


def test_profile_defaults_are_local_and_round_trip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    defaults = store.load()
    assert defaults.theme == "system"
    assert defaults.simple_language is False
    saved = store.update(
        {
            "theme": "dark",
            "nickname": "Alex",
            "gender_identity": "non-binary",
            "gendered_language": False,
            "knowledge_level": "developer",
            "simple_language": True,
        }
    )
    assert saved.theme == "dark"
    assert store.load() == saved
    assert (tmp_path / "profile" / "preferences.json").stat().st_mode & 0o077 == 0


def test_profile_accepts_accessibility_and_voice_preferences(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    saved = store.update(
        {
            "voice_input": True,
            "voice_output": True,
            "wake_phrase_enabled": True,
            "wake_phrase": "Hey Kumpel",
            "screen_reader": True,
            "braille_input": True,
            "color_blind_mode": "red-green",
        }
    )
    assert saved.wake_phrase == "Hey Kumpel"
    assert saved.screen_reader is True
    assert saved.color_blind_mode == "red-green"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("theme", "neon"),
        ("knowledge_level", "guru"),
        ("age", 2),
        ("input_volume", 101),
        ("wake_phrase", "x" * 81),
        ("unknown", True),
    ],
)
def test_profile_rejects_invalid_values(tmp_path: Path, name: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProfileStore(tmp_path).update({name: value})


def test_profile_rejects_symlink(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    external = tmp_path / "external.json"
    external.write_text("{}", encoding="utf-8")
    os.symlink(external, store.path)
    with pytest.raises(RuntimeError):
        store.load()
