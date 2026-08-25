from __future__ import annotations

import threading
from pathlib import Path

import pytest

from autonomous_agent.core.learning import (
    LearningService,
    ResearchSource,
    _parse_feed,
    _validate_research_url,
)


def _service(tmp_path: Path, *, network_enabled: bool = True, fetcher=None) -> LearningService:
    project = tmp_path / "project"
    project.mkdir()
    return LearningService(
        tmp_path / "state",
        project,
        network_enabled=network_enabled,
        interval_s=300,
        fetcher=fetcher,
    )


def test_offline_research_is_explicit_and_does_not_fetch(tmp_path: Path) -> None:
    called = False

    def fetch(_source: ResearchSource) -> bytes:
        nonlocal called
        called = True
        return b""

    service = _service(tmp_path, network_enabled=False, fetcher=fetch)
    result = service.research_now()
    assert result["status"] == "offline"
    assert called is False
    assert service.status().network_available is False


def test_trusted_feed_creates_knowledge_and_gated_proposals(tmp_path: Path) -> None:
    payload = b"""<?xml version='1.0'?><rss><channel><item>
      <title>New local AI safety technique</title>
      <link>https://arxiv.org/abs/1234.5678</link>
      <description>bounded and testable</description>
      <pubDate>2026-08-24</pubDate>
    </item></channel></rss>"""

    def fetch(_source: ResearchSource) -> bytes:
        return payload

    service = _service(tmp_path, fetcher=fetch)
    result = service.research_now()
    assert result["items"] == 1
    assert len(service.store.knowledge()) == 1
    assert len(service.store.suggestions()) == 1
    assert all(item.release_gate_required for item in service.store.suggestions())
    assert all(not item.auto_apply for item in service.store.suggestions())
    assert len(service.store.self_updates()) == 1


def test_invalid_feed_link_is_ignored(tmp_path: Path) -> None:
    payload = b"<rss><channel><item><title>bad</title><link>http://evil.invalid/x</link></item></channel></rss>"
    source = ResearchSource("test", "https://arxiv.org/rss/cs.AI", "ai")
    assert _parse_feed(payload, source) == ()


def test_feed_dtd_and_entity_declarations_are_rejected(tmp_path: Path) -> None:
    source = ResearchSource("test", "https://arxiv.org/rss/cs.AI", "ai")
    payload = b"<!DOCTYPE rss [<!ENTITY x 'expanded'>]><rss/>"
    with pytest.raises(ValueError, match="forbidden XML"):
        _parse_feed(payload, source)


def test_research_url_rejects_non_https_ports() -> None:
    with pytest.raises(ValueError, match="port"):
        _validate_research_url("https://arxiv.org:8443/rss/cs.AI")


def test_research_service_allows_only_one_in_flight_run(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def fetch(_source: ResearchSource) -> bytes:
        started.set()
        release.wait(timeout=2)
        return b"<rss><channel/></rss>"

    service = _service(tmp_path, fetcher=fetch)
    worker = threading.Thread(target=service.research_now)
    worker.start()
    assert started.wait(timeout=1)
    assert service.research_now()["status"] == "busy"
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_accounts_store_hashes_and_exposes_only_public_sync_manifest(tmp_path: Path) -> None:
    service = _service(tmp_path, network_enabled=False)
    account = service.create_account("owner", "correct horse battery")
    assert service.authenticate("owner", "correct horse battery") == account
    assert service.authenticate("owner", "wrong password") is None
    public = service.sync_manifest()
    assert public["device_id"] == account.device_id
    assert "password_hash" not in str(public)
    assert "password_salt" not in str(public)
    with pytest.raises(ValueError):
        service.create_account("owner", "another password")


def test_account_recovery_requires_security_answer_and_rehashes_password(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, network_enabled=False)
    account = service.create_account(
        "owner",
        "correct horse battery",
        security_question="Lieblingsfarbe?",
        security_answer="Blau",
    )
    assert account.to_public_dict()["recovery_configured"] is True
    assert service.reset_password("owner", "falsch", "new correct password") is None
    updated = service.reset_password("owner", "blau", "new correct password")
    assert updated is not None
    assert service.authenticate("owner", "correct horse battery") is None
    assert service.authenticate("owner", "new correct password") == updated


def test_completed_task_review_is_persisted_as_improvement(tmp_path: Path) -> None:
    service = _service(tmp_path, network_enabled=False)
    service.review_task(
        "Erstelle eine Datei",
        {"completion": {"completed": True}},
    )
    suggestions = service.store.suggestions()
    assert len(suggestions) == 1
    assert suggestions[0].kind == "task-review"
    assert suggestions[0].release_gate_required is True


def test_failed_task_review_becomes_high_priority_learning_lead(tmp_path: Path) -> None:
    service = _service(tmp_path, network_enabled=False)
    service.review_task(
        "Implementiere sichere Wiederaufnahme",
        {
            "completion": {
                "completed": False,
                "criteria": [
                    {"criterion_id": "recovery", "passed": False},
                    {"criterion_id": "tests", "passed": True},
                ],
            }
        },
    )
    suggestion = service.store.suggestions()[0]
    assert suggestion.priority == "high"
    assert "recovery" in suggestion.description
    assert suggestion.auto_apply is False


def test_feedback_becomes_gated_improvement_proposal(tmp_path: Path) -> None:
    service = _service(tmp_path, network_enabled=False)
    service.record_feedback("session-123", 3, "Die Erklärung war zu knapp.")
    suggestion = service.store.suggestions()[0]
    assert suggestion.kind == "user-feedback"
    assert suggestion.priority == "high"
    assert "zu knapp" in suggestion.description
    assert suggestion.release_gate_required is True
