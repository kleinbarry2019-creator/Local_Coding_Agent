from __future__ import annotations

from autonomous_agent.core.voice import VoiceService


def test_voice_status_is_local_only(monkeypatch) -> None:
    monkeypatch.setattr("autonomous_agent.core.voice.shutil.which", lambda name: "/usr/bin/" + name)
    service = VoiceService()
    status = service.status()
    assert status.output_available is True
    assert status.input_available is True
    assert status.network_used is False


def test_voice_rejects_unbounded_text(monkeypatch) -> None:
    monkeypatch.setattr("autonomous_agent.core.voice.shutil.which", lambda _name: "/usr/bin/espeak-ng")
    service = VoiceService()
    try:
        service.speak("x" * 9_000)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized text was accepted")


def test_voice_reports_missing_output(monkeypatch) -> None:
    monkeypatch.setattr("autonomous_agent.core.voice.shutil.which", lambda _name: None)
    result = VoiceService().speak("Hallo ACB")
    assert result.started is False
    assert result.diagnostic == "output-engine-unavailable"


def test_wake_phrase_matching_is_local_and_tolerates_punctuation() -> None:
    assert VoiceService.wake_phrase_matches("Hey Kumpel, starte", "Hey Kumpel")
    assert VoiceService.wake_phrase_matches("HEY KUMPEL", "Hey Kumpel")
    assert not VoiceService.wake_phrase_matches("Hallo Kumpel", "Hey Kumpel")
    assert VoiceService.remove_wake_phrase(
        "Hey Kumpel, erstelle notes.txt", "Hey Kumpel"
    ) == "erstelle notes.txt"
