from __future__ import annotations

from pathlib import Path

import pytest

from autonomous_agent.core.preferences import ProfileStore
from autonomous_agent.core.user_experience import (
    OnboardingService,
    TaskFeedbackStore,
    build_response_context,
    detect_assistive_hints,
    detect_voice_capabilities,
)


def test_onboarding_trial_and_completion_are_bounded(tmp_path: Path) -> None:
    service = OnboardingService(ProfileStore(tmp_path))
    assert service.status().phase == "welcome"
    trial = service.start_trial()
    assert trial.phase == "trial"
    assert trial.trial_active is True
    assert service.complete().phase == "ready"


def test_onboarding_rejects_unreasonable_trial(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        OnboardingService(ProfileStore(tmp_path)).start_trial(days=31)


def test_response_context_reflects_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    preferences = store.update(
        {
            "simple_language": True,
            "knowledge_level": "beginner",
            "nickname": "Alex",
            "pronouns": "dey/deren",
            "age": 72,
        }
    )
    context = build_response_context(preferences)
    assert context.nickname == "Alex"
    assert context.pronouns == "dey/deren"
    assert context.simple_language is True
    assert "nummeriert" in context.next_steps


def test_assistive_hints_are_non_invasive() -> None:
    hints = detect_assistive_hints(
        {
            "ORCA_HOST": "1",
            "BRLTTY_BAUD": "9600",
            "GDK_SCALE": "2",
            "GTK_THEME": "HighContrast",
            "ACB_MOTOR_ASSISTANCE": "1",
        }
    )
    assert hints.screen_reader is True
    assert hints.braille is True
    assert hints.large_text is True
    assert hints.high_contrast is True
    assert hints.motor_assistance is True
    assert hints.source == "local-hints-only"


def test_voice_detection_is_bounded() -> None:
    capabilities = detect_voice_capabilities()
    assert {item.direction for item in capabilities} == {"input", "output"}
    assert all(item.offline for item in capabilities)


def test_feedback_is_validated_and_persisted(tmp_path: Path) -> None:
    store = TaskFeedbackStore(tmp_path)
    item = store.add("session-0123456789abcdef0123456789abcdef", 9, "Sehr gut")
    assert item.rating == 9
    assert store.items()[0] == item
    with pytest.raises(ValueError):
        store.add(item.session_id, 11)
    with pytest.raises(ValueError):
        store.add(item.session_id, 8, "x" * 2_001)
