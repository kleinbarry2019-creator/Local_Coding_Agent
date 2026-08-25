"""Local-first learning, research, improvement, and account primitives.

The learning surface is deliberately separate from task execution.  Research
is bounded to an allow-listed set of HTTPS feeds, stored locally, and converted
into reviewable proposals.  A proposal never becomes executable code merely
because a remote article exists; it must pass the same release and security
gates as any other change.  This keeps continuous improvement useful without
turning the network into an untrusted code execution channel.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - DTD/entity declarations are rejected below
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, cast

MAX_FEED_BYTES = 512_000
MAX_ITEMS_PER_FEED = 20
MAX_KNOWLEDGE_ITEMS = 300
MAX_SUGGESTIONS = 200
MAX_SELF_UPDATES = 100
MAX_EXPERIENCES = 200
MAX_GATE_EVIDENCE = 12
MAX_GATE_EVIDENCE_TEXT = 240
RESEARCH_INTERVAL_S = 6 * 60 * 60
_USERNAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
_ALLOWED_HOSTS = frozenset(
    {
        "arxiv.org",
        "www.arxiv.org",
        "www.cisa.gov",
        "www.nist.gov",
        "www.ollama.com",
        "ollama.com",
    }
)


@dataclass(frozen=True)
class ResearchSource:
    name: str
    url: str
    topic: str


RESEARCH_SOURCES: tuple[ResearchSource, ...] = (
    ResearchSource("arXiv KI", "https://arxiv.org/rss/cs.AI", "ai"),
    ResearchSource("arXiv ML", "https://arxiv.org/rss/cs.LG", "ai"),
    ResearchSource(
        "NIST Cybersecurity",
        "https://www.nist.gov/news-events/cybersecurity/rss.xml",
        "security",
    ),
    ResearchSource(
        "CISA Advisories",
        "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        "security",
    ),
)


@dataclass(frozen=True)
class KnowledgeItem:
    item_id: str
    title: str
    url: str
    source: str
    topic: str
    summary: str
    published_at: str | None
    discovered_at: str
    trust: str

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "topic": self.topic,
            "summary": self.summary,
            "published_at": self.published_at,
            "discovered_at": self.discovered_at,
            "trust": self.trust,
        }


@dataclass(frozen=True)
class ExperienceItem:
    """A bounded local lesson extracted from a completed task review."""

    experience_id: str
    goal: str
    outcome: str
    lesson: str
    failed_criteria: tuple[str, ...]
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experience_id": self.experience_id,
            "goal": self.goal,
            "outcome": self.outcome,
            "lesson": self.lesson,
            "failed_criteria": list(self.failed_criteria),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ImprovementSuggestion:
    suggestion_id: str
    kind: str
    title: str
    description: str
    source_ids: tuple[str, ...]
    status: str
    priority: str
    auto_apply: bool
    release_gate_required: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "suggestion_id": self.suggestion_id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "source_ids": list(self.source_ids),
            "status": self.status,
            "priority": self.priority,
            "auto_apply": self.auto_apply,
            "release_gate_required": self.release_gate_required,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SelfUpdateProposal:
    update_id: str
    title: str
    reason: str
    source_ids: tuple[str, ...]
    status: str
    gate_status: str
    created_at: str
    updated_at: str
    verification_evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "update_id": self.update_id,
            "title": self.title,
            "reason": self.reason,
            "source_ids": list(self.source_ids),
            "status": self.status,
            "gate_status": self.gate_status,
            "verification_evidence": list(self.verification_evidence),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class UserAccount:
    account_id: str
    username: str
    role: str
    created_at: str
    device_id: str
    sync_scope: str
    password_salt: str
    password_hash: str
    security_question: str = ""
    security_answer_salt: str = ""
    security_answer_hash: str = ""

    def to_public_dict(self) -> dict[str, object]:
        """Return an account document safe for the GUI and future sync."""
        return {
            "account_id": self.account_id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
            "device_id": self.device_id,
            "sync_scope": self.sync_scope,
            "recovery_configured": bool(self.security_question and self.security_answer_hash),
        }


@dataclass(frozen=True)
class LearningStatus:
    last_research_at: str | None
    next_research_at: str | None
    network_enabled: bool
    network_available: bool
    knowledge_count: int
    suggestion_count: int
    self_update_count: int
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "last_research_at": self.last_research_at,
            "next_research_at": self.next_research_at,
            "network_enabled": self.network_enabled,
            "network_available": self.network_available,
            "knowledge_count": self.knowledge_count,
            "suggestion_count": self.suggestion_count,
            "self_update_count": self.self_update_count,
            "last_error": self.last_error,
        }


class LearningStore:
    """Owner-controlled, atomic JSON storage beneath the validated state root."""

    def __init__(self, state_root: Path) -> None:
        self.root = state_root.resolve(strict=False) / "learning"
        self._lock = threading.RLock()
        _ensure_directory(self.root)
        self._ensure_meta()

    def knowledge(self, limit: int = 50) -> tuple[KnowledgeItem, ...]:
        raw = self._read_list("knowledge.json")
        items = [_knowledge_from_dict(item) for item in raw if isinstance(item, dict)]
        return tuple(items[:_bounded_limit(limit, MAX_KNOWLEDGE_ITEMS)])

    def experiences(self, limit: int = 50) -> tuple[ExperienceItem, ...]:
        raw = self._read_list("experiences.json")
        items = [_experience_from_dict(item) for item in raw if isinstance(item, dict)]
        return tuple(items[:_bounded_limit(limit, MAX_EXPERIENCES)])

    def suggestions(self, limit: int = 50) -> tuple[ImprovementSuggestion, ...]:
        raw = self._read_list("suggestions.json")
        items = [_suggestion_from_dict(item) for item in raw if isinstance(item, dict)]
        return tuple(items[:_bounded_limit(limit, MAX_SUGGESTIONS)])

    def self_updates(self, limit: int = 50) -> tuple[SelfUpdateProposal, ...]:
        raw = self._read_list("self_updates.json")
        items = [_update_from_dict(item) for item in raw if isinstance(item, dict)]
        return tuple(items[:_bounded_limit(limit, MAX_SELF_UPDATES)])

    def accounts(self) -> tuple[UserAccount, ...]:
        raw = self._read_list("accounts.json")
        return tuple(_account_from_dict(item) for item in raw if isinstance(item, dict))

    def add_knowledge(self, item: KnowledgeItem) -> bool:
        with self._lock:
            raw = self._read_list("knowledge.json")
            if any(isinstance(value, dict) and value.get("item_id") == item.item_id for value in raw):
                return False
            values = [item.to_dict(), *raw]
            self._write_list("knowledge.json", values[:MAX_KNOWLEDGE_ITEMS])
            return True

    def add_experience(self, item: ExperienceItem) -> bool:
        with self._lock:
            raw = self._read_list("experiences.json")
            if any(
                isinstance(value, dict)
                and value.get("experience_id") == item.experience_id
                for value in raw
            ):
                return False
            self._write_list(
                "experiences.json", [item.to_dict(), *raw][:MAX_EXPERIENCES]
            )
            return True

    def add_suggestion(self, suggestion: ImprovementSuggestion) -> bool:
        with self._lock:
            raw = self._read_list("suggestions.json")
            if any(
                isinstance(value, dict)
                and value.get("title") == suggestion.title
                and value.get("status") not in {"rejected", "superseded"}
                for value in raw
            ):
                return False
            self._write_list(
                "suggestions.json", [suggestion.to_dict(), *raw][:MAX_SUGGESTIONS]
            )
            return True

    def add_self_update(self, update: SelfUpdateProposal) -> bool:
        with self._lock:
            raw = self._read_list("self_updates.json")
            if any(
                isinstance(value, dict)
                and value.get("title") == update.title
                and value.get("status") not in {"rejected", "superseded"}
                for value in raw
            ):
                return False
            self._write_list(
                "self_updates.json", [update.to_dict(), *raw][:MAX_SELF_UPDATES]
            )
            return True

    def record_self_update_gate(
        self,
        update_id: str,
        *,
        gate_status: str,
        evidence: Sequence[str],
    ) -> SelfUpdateProposal:
        """Persist bounded verification evidence for a self-update proposal."""
        if gate_status not in {"passed", "failed"}:
            raise ValueError("gate status is invalid")
        if type(update_id) is not str or not update_id:
            raise ValueError("update id is invalid")
        normalized = tuple(
            item.strip()[:MAX_GATE_EVIDENCE_TEXT]
            for item in evidence
            if isinstance(item, str) and item.strip()
        )[:MAX_GATE_EVIDENCE]
        if gate_status == "passed" and not _gate_evidence_complete(normalized):
            raise ValueError("successful gate evidence is incomplete")
        with self._lock:
            raw = self._read_list("self_updates.json")
            for index, value in enumerate(raw):
                if not isinstance(value, dict) or value.get("update_id") != update_id:
                    continue
                current = _update_from_dict(value)
                updated = replace(
                    current,
                    status="verified" if gate_status == "passed" else "candidate",
                    gate_status=gate_status,
                    verification_evidence=normalized,
                    updated_at=_timestamp(),
                )
                raw[index] = updated.to_dict()
                self._write_list("self_updates.json", raw)
                return updated
        raise ValueError("self-update does not exist")

    def add_account(self, account: UserAccount) -> None:
        with self._lock:
            raw = self._read_list("accounts.json")
            if any(
                isinstance(value, dict)
                and value.get("username", "").casefold() == account.username.casefold()
                for value in raw
            ):
                raise ValueError("username already exists")
            self._write_list("accounts.json", [asdict_account(account), *raw])

    def update_account(self, account: UserAccount) -> None:
        """Atomically replace one account without exposing credential material."""
        with self._lock:
            raw = self._read_list("accounts.json")
            replaced = False
            updated: list[object] = []
            for value in raw:
                if isinstance(value, dict) and value.get("account_id") == account.account_id:
                    updated.append(asdict_account(account))
                    replaced = True
                else:
                    updated.append(value)
            if not replaced:
                raise ValueError("account does not exist")
            self._write_list("accounts.json", updated)

    def metadata(self) -> dict[str, object]:
        value = self._read("meta.json", {})
        return value if isinstance(value, dict) else {}

    def update_metadata(self, values: Mapping[str, object]) -> None:
        with self._lock:
            current = self.metadata()
            current.update(values)
            self._write("meta.json", current)

    def _ensure_meta(self) -> None:
        with self._lock:
            meta = self.metadata()
            if "device_id" not in meta or not isinstance(meta["device_id"], str):
                self._write("meta.json", {"device_id": f"device-{uuid.uuid4().hex}"})

    def _read_list(self, name: str) -> list[object]:
        value = self._read(name, [])
        return value if isinstance(value, list) else []

    def _read(self, name: str, default: object) -> object:
        path = self.root / name
        try:
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return default
        except OSError as error:
            raise RuntimeError("learning state could not be opened safely") from error
        try:
            metadata = os.fstat(descriptor)
            _validate_file_metadata(metadata)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                return json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("learning state is invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_list(self, name: str, values: Sequence[object]) -> None:
        self._write(name, list(values))

    def _write(self, name: str, value: object) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4_000_000:
            raise ValueError("learning state is too large")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / name)
            temporary = ""
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


class LearningService:
    """Research trusted feeds and turn observations into gated proposals."""

    def __init__(
        self,
        state_root: Path,
        project_root: Path,
        *,
        network_enabled: bool = True,
        interval_s: int = RESEARCH_INTERVAL_S,
        fetcher: Callable[[ResearchSource], bytes] | None = None,
    ) -> None:
        self.store = LearningStore(state_root)
        self.project_root = project_root.resolve(strict=True)
        self.network_enabled = network_enabled
        self.interval_s = max(300, min(interval_s, 7 * 24 * 60 * 60))
        self._fetcher = _fetch_feed if fetcher is None else fetcher
        self._lock = threading.RLock()
        self._research_running = False

    def status(self) -> LearningStatus:
        metadata = self.store.metadata()
        last = _optional_string(metadata.get("last_research_at"))
        next_run = None
        if last is not None:
            try:
                next_run = (
                    datetime.fromisoformat(last) + timedelta(seconds=self.interval_s)
                ).isoformat(timespec="seconds")
            except ValueError:
                next_run = None
        return LearningStatus(
            last_research_at=last,
            next_research_at=next_run,
            network_enabled=self.network_enabled,
            network_available=bool(metadata.get("network_available", False)),
            knowledge_count=len(self.store.knowledge(MAX_KNOWLEDGE_ITEMS)),
            suggestion_count=len(self.store.suggestions(MAX_SUGGESTIONS)),
            self_update_count=len(self.store.self_updates(MAX_SELF_UPDATES)),
            last_error=_optional_string(metadata.get("last_error")),
        )

    def run_due(self) -> dict[str, object] | None:
        metadata = self.store.metadata()
        last = _optional_string(metadata.get("last_research_at"))
        if last is not None:
            try:
                if datetime.now(UTC) < datetime.fromisoformat(last) + timedelta(seconds=self.interval_s):
                    return None
            except ValueError:
                pass
        return self.research_now()

    def research_now(self) -> dict[str, object]:
        with self._lock:
            if self._research_running:
                return {
                    "status": "busy",
                    "started_at": _timestamp(),
                    "finished_at": _timestamp(),
                    "sources": 0,
                    "items": 0,
                    "message": "research already running",
                }
            self._research_running = True
        try:
            return self._research_now()
        finally:
            with self._lock:
                self._research_running = False

    def _research_now(self) -> dict[str, object]:
        started = _timestamp()
        if not self.network_enabled:
            result = {
                "status": "offline",
                "started_at": started,
                "finished_at": _timestamp(),
                "sources": 0,
                "items": 0,
                "message": "network research disabled; local knowledge remains available",
            }
            self.store.update_metadata(
                {
                    "last_research_at": started,
                    "network_available": False,
                    "last_error": "offline-mode",
                }
            )
            return result

        source_results: list[dict[str, object]] = []
        added = 0
        errors: list[str] = []
        for source in RESEARCH_SOURCES:
            try:
                items = _parse_feed(self._fetcher(source), source)
                source_added = 0
                for item in items:
                    if self.store.add_knowledge(item):
                        source_added += 1
                        added += 1
                        self._create_proposals(item)
                source_results.append({"source": source.name, "items": source_added, "status": "ok"})
            except (OSError, ValueError, ET.ParseError, urllib.error.URLError) as error:
                errors.append(source.name)
                source_results.append({"source": source.name, "items": 0, "status": "unavailable"})
                del error
        self.store.update_metadata(
            {
                "last_research_at": started,
                "network_available": not errors,
                "last_error": None if not errors else "feeds-unavailable",
            }
        )
        return {
            "status": "ok" if not errors else "partial",
            "started_at": started,
            "finished_at": _timestamp(),
            "sources": source_results,
            "items": added,
            "errors": errors,
        }

    def review_task(self, goal: str, result: Mapping[str, object]) -> None:
        """Turn verified outcomes and failures into reviewable learning leads.

        Failed or incomplete work is valuable training data too: it identifies
        which planner, tool, or acceptance criterion needs attention.  The
        resulting proposal is still advisory and can never mutate code by
        itself.
        """
        completion = result.get("completion")
        if not isinstance(completion, Mapping):
            return
        completed = completion.get("completed") is True
        criteria = completion.get("criteria", ())
        failed_criteria = tuple(
            str(item.get("criterion_id", "unknown"))
            for item in criteria
            if isinstance(item, Mapping) and item.get("passed") is False
        )
        now = _timestamp()
        context = self.knowledge_context(goal, limit=5)
        source_ids = tuple(
            str(item["item_id"])
            for item in context
            if isinstance(item.get("item_id"), str)
        )
        outcome = "completed" if completed else "failed"
        lesson = (
            "A verified task can be reused as a regression-tested workflow."
            if completed
            else "Reproduce the missing criteria before changing the planner or tools."
        )
        self.store.add_experience(
            ExperienceItem(
                experience_id=f"experience-{uuid.uuid4().hex}",
                goal=goal[:400],
                outcome=outcome,
                lesson=lesson,
                failed_criteria=failed_criteria[:8],
                created_at=now,
            )
        )
        if not completed:
            self._detect_recurring_failure(failed_criteria)
        if completed:
            title = f"Nachprüfung: {goal[:96]}"
            description = (
                "Auftrag wurde vollständig verifiziert. Prüfe regelmäßig, ob "
                "ein Regressionstest, eine Dokumentationsverbesserung oder "
                "eine sicherere Automatisierung daraus entstehen kann."
            )
            priority = "normal"
        else:
            failed = ", ".join(failed_criteria[:8]) or "unbekannte Kriterien"
            title = f"Fehleranalyse: {goal[:88]}"
            description = (
                "Auftrag wurde nicht vollständig bestätigt. Analysiere die "
                f"fehlenden Akzeptanzkriterien ({failed}), reproduziere den "
                "Fehler und ergänze danach einen Regressionstest."
            )
            priority = "high"
        self.store.add_suggestion(
            ImprovementSuggestion(
                suggestion_id=f"suggestion-{uuid.uuid4().hex}",
                kind="task-review",
                title=title,
                description=description,
                source_ids=source_ids,
                status="candidate",
                priority=priority,
                auto_apply=False,
                release_gate_required=True,
                created_at=now,
                updated_at=now,
            )
        )
    def _detect_recurring_failure(self, failed_criteria: Sequence[str]) -> None:
        """Create one bounded root-cause lead for repeated failed criteria."""
        experiences = self.store.experiences(MAX_EXPERIENCES)
        for criterion in tuple(item for item in failed_criteria if item):
            matches = tuple(
                item
                for item in experiences
                if criterion in item.failed_criteria and item.outcome == "failed"
            )
            if len(matches) < 2:
                continue
            now = _timestamp()
            self.store.add_suggestion(
                ImprovementSuggestion(
                    suggestion_id=f"suggestion-{uuid.uuid4().hex}",
                    kind="recurring-failure",
                    title=f"Wiederkehrendes Problem: {criterion[:80]}",
                    description=(
                        f"Das Kriterium {criterion} ist in mindestens "
                        f"{len(matches)} Aufträgen fehlgeschlagen. Ermittle die "
                        "gemeinsame Ursache in Planner, Tool oder Umgebung, "
                        "ergänze einen Regressionstest und prüfe danach das "
                        "vollständige Release-Gate."
                    ),
                    source_ids=tuple(item.experience_id for item in matches[:8]),
                    status="candidate",
                    priority="high",
                    auto_apply=False,
                    release_gate_required=True,
                    created_at=now,
                    updated_at=now,
                )
            )

    def knowledge_context(
        self, goal: str, *, limit: int = 5
    ) -> tuple[dict[str, object], ...]:
        """Return bounded, relevance-ranked local knowledge for a goal.

        This is deliberately lexical and explainable.  Remote content is only
        treated as reference metadata; it is never interpreted as executable
        instructions or injected into a shell command.
        """
        if type(goal) is not str or not goal.strip() or len(goal) > 4_000:
            raise ValueError("learning goal is invalid")
        if type(limit) is not int or limit < 0:
            raise ValueError("learning limit is invalid")
        wanted = _learning_tokens(goal)
        ranked: list[tuple[float, KnowledgeItem]] = []
        for item in self.store.knowledge(MAX_KNOWLEDGE_ITEMS):
            haystack = _learning_tokens(
                f"{item.title} {item.summary} {item.topic} {item.source}"
            )
            overlap = wanted & haystack
            if not overlap:
                continue
            score = len(overlap) / max(1, len(wanted))
            if item.trust == "allow-listed-feed":
                score += 0.1
            ranked.append((score, item))
        ranked.sort(key=lambda value: (-value[0], value[1].discovered_at))
        bounded = ranked[: min(limit, 20)]
        knowledge_context: list[dict[str, object]] = [
            {
                **item.to_dict(),
                "relevance": round(score, 4),
            }
            for score, item in bounded
        ]
        experience_ranked: list[tuple[float, ExperienceItem]] = []
        for experience in self.store.experiences(MAX_EXPERIENCES):
            overlap = wanted & _learning_tokens(
                f"{experience.goal} {experience.lesson}"
            )
            if overlap:
                experience_ranked.append(
                    (len(overlap) / max(1, len(wanted)) + 0.05, experience)
                )
        experience_ranked.sort(key=lambda value: (-value[0], value[1].created_at))
        experience_context: list[dict[str, object]] = [
            {
                "item_id": experience.experience_id,
                "title": f"ACB-Erfahrung: {experience.goal[:120]}",
                "url": "",
                "source": "lokales Erfahrungs-Gedächtnis",
                "topic": "experience",
                "summary": experience.lesson,
                "published_at": None,
                "discovered_at": experience.created_at,
                "trust": "local-experience",
                "outcome": experience.outcome,
                "failed_criteria": list(experience.failed_criteria),
                "relevance": round(score, 4),
            }
            for score, experience in experience_ranked[: min(limit, 20)]
        ]
        return tuple((experience_context + knowledge_context)[: min(limit, 20)])

    def record_feedback(
        self,
        session_id: str,
        rating: int,
        comment: str = "",
        *,
        user_id: str = "local-profile",
    ) -> None:
        """Convert explicit user feedback into a bounded improvement proposal."""
        if type(rating) is not int or not 1 <= rating <= 10:
            raise ValueError("feedback rating is invalid")
        if type(session_id) is not str or not session_id:
            raise ValueError("feedback session is invalid")
        if type(comment) is not str:
            raise ValueError("feedback comment is invalid")
        if type(user_id) is not str or not user_id or len(user_id) > 160:
            raise ValueError("feedback user is invalid")
        note = " ".join(comment.split())[:600]
        description = (
            f"Nutzerbewertung für {session_id}: {rating}/10. "
            "Prüfe die zugehörige Antwort, Akzeptanzkriterien und Tests; "
            "übernehme keine Änderung ohne Release-Gate."
        )
        if note:
            description += f" Kommentar: {note}"
        now = _timestamp()
        self.store.add_suggestion(
            ImprovementSuggestion(
                suggestion_id=f"suggestion-{uuid.uuid4().hex}",
                kind="user-feedback",
                title=f"Nutzerfeedback auswerten: {session_id[-16:]}",
                description=description,
                source_ids=(),
                status="candidate",
                priority="high" if rating <= 5 else "normal",
                auto_apply=False,
                release_gate_required=True,
                created_at=now,
                updated_at=now,
            )
        )
        metadata = self.store.metadata()
        raw_by_user = metadata.get("low_feedback_by_user", {})
        by_user = dict(raw_by_user) if isinstance(raw_by_user, dict) else {}
        low_feedback_count = by_user.get(user_id, 0)
        if type(low_feedback_count) is not int or low_feedback_count < 0:
            low_feedback_count = 0
        if rating <= 5:
            low_feedback_count += 1
        by_user[user_id] = low_feedback_count
        by_user = dict(list(by_user.items())[-100:])
        raw_ratings = metadata.get("last_feedback_rating_by_user", {})
        ratings_by_user: dict[str, int] = {}
        if isinstance(raw_ratings, dict):
            ratings_by_user = {
                key: value
                for key, value in raw_ratings.items()
                if isinstance(key, str) and type(value) is int
            }
        ratings_by_user[user_id] = rating
        ratings_by_user = dict(list(ratings_by_user.items())[-100:])
        feedback_hint = _feedback_hint(note) if rating <= 6 else None
        raw_hints = metadata.get("feedback_hints_by_user", {})
        hints_by_user = dict(raw_hints) if isinstance(raw_hints, dict) else {}
        user_hints = dict(hints_by_user.get(user_id, {}))
        if feedback_hint is not None:
            user_hints[feedback_hint] = user_hints.get(feedback_hint, 0) + 1
            user_hints = dict(list(user_hints.items())[-8:])
            hints_by_user[user_id] = user_hints
        hints_by_user = dict(list(hints_by_user.items())[-100:])
        self.store.update_metadata(
            {
                "low_feedback_by_user": by_user,
                "last_feedback_rating_by_user": ratings_by_user,
                "feedback_hints_by_user": hints_by_user,
            }
        )
        if low_feedback_count >= 2:
            self.store.add_suggestion(
                ImprovementSuggestion(
                    suggestion_id=f"suggestion-{uuid.uuid4().hex}",
                    kind="recurring-feedback",
                    title=f"Wiederkehrendes negatives Nutzerfeedback: {user_id}",
                    description=(
                        f"Mindestens {low_feedback_count} niedrige Bewertungen "
                        "wurden erfasst. Vergleiche die betroffenen Aufgaben, "
                        "identifiziere ein gemeinsames Verständlichkeits- oder "
                        "Qualitätsproblem und ergänze einen Regressionstest."
                    ),
                    source_ids=(),
                    status="candidate",
                    priority="high",
                    auto_apply=False,
                    release_gate_required=True,
                    created_at=now,
                    updated_at=now,
                )
            )

    def response_hint(self, user_id: str = "local-profile") -> str:
        """Return a conservative, explainable style hint learned from feedback."""
        profile = self.response_hint_profile(user_id)
        preferred = profile.get("preferred_hint")
        return (
            {
                "more-detail": "mehr Details und Beispiele geben",
                "simple-language": "einfacher und schrittweise formulieren",
                "concise": "kürzer und direkter antworten",
                "technical": "technischer und mit prüfbaren Details antworten",
            }.get(preferred, "")
            if isinstance(preferred, str)
            else ""
        )

    def response_hint_profile(self, user_id: str = "local-profile") -> dict[str, object]:
        """Describe the per-user feedback signal used for response adaptation."""
        if type(user_id) is not str or not user_id:
            return {"user_id": "", "hints": {}, "preferred_hint": None, "confidence": 0.0}
        raw = self.store.metadata().get("feedback_hints_by_user", {})
        if not isinstance(raw, dict):
            return {"user_id": user_id, "hints": {}, "preferred_hint": None, "confidence": 0.0}
        values = raw.get(user_id)
        if not isinstance(values, dict):
            return {"user_id": user_id, "hints": {}, "preferred_hint": None, "confidence": 0.0}
        counts = {
            key: value
            for key, value in values.items()
            if isinstance(key, str) and type(value) is int and value > 0
        }
        if not counts:
            return {"user_id": user_id, "hints": {}, "preferred_hint": None, "confidence": 0.0}
        hint = max(counts, key=lambda key: counts[key])
        total = sum(counts.values())
        return {
            "user_id": user_id,
            "hints": counts,
            "preferred_hint": hint,
            "confidence": round(counts[hint] / max(1, total), 3),
        }

    def record_gate_result(
        self,
        update_id: str,
        *,
        gate_status: str,
        evidence: Sequence[str],
    ) -> SelfUpdateProposal:
        """Record release evidence before a self-update can be verified."""
        return self.store.record_self_update_gate(
            update_id,
            gate_status=gate_status,
            evidence=evidence,
        )

    def _create_proposals(self, item: KnowledgeItem) -> None:
        now = _timestamp()
        kind = "security-research" if item.topic == "security" else "ai-research"
        self.store.add_suggestion(
            ImprovementSuggestion(
                suggestion_id=f"suggestion-{uuid.uuid4().hex}",
                kind=kind,
                title=f"Wissen prüfen: {item.title[:100]}",
                description=(
                    "Quelle wurde aus einem vertrauenswürdigen Feed übernommen. "
                    "Bewerte Relevanz für ACB und erstelle erst danach eine "
                    "getestete, rückrollbare Änderung."
                ),
                source_ids=(item.item_id,),
                status="candidate",
                priority="high" if item.topic == "security" else "normal",
                auto_apply=False,
                release_gate_required=True,
                created_at=now,
                updated_at=now,
            )
        )
        self.store.add_self_update(
            SelfUpdateProposal(
                update_id=f"update-{uuid.uuid4().hex}",
                title=f"ACB-Weiterentwicklung: {item.title[:100]}",
                reason=(
                    "Neue externe Information wurde erkannt. Die mögliche Änderung "
                    "muss mit Hardware-/Software-Limits, Tests, Security-Scan und "
                    "Rollback verifiziert werden."
                ),
                source_ids=(item.item_id,),
                status="candidate",
                gate_status="not-run",
                created_at=now,
                updated_at=now,
            )
        )

    def create_account(
        self,
        username: str,
        password: str,
        *,
        role: str = "owner",
        security_question: str = "",
        security_answer: str = "",
    ) -> UserAccount:
        if not _USERNAME.fullmatch(username) or type(password) is not str or not 10 <= len(password) <= 256:
            raise ValueError("account credentials are invalid")
        if role not in {"owner", "operator"}:
            raise ValueError("account role is invalid")
        question = security_question.strip()
        answer = security_answer.strip()
        if question and not 3 <= len(question) <= 200:
            raise ValueError("security question is invalid")
        if bool(question) != bool(answer) or answer and not 3 <= len(answer) <= 256:
            raise ValueError("security answer is invalid")
        salt = secrets.token_bytes(16)
        password_hash = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=16_384, r=8, p=1, dklen=32
        )
        answer_salt = secrets.token_bytes(16) if answer else b""
        answer_hash = (
            hashlib.scrypt(
                answer.casefold().encode("utf-8"),
                salt=answer_salt,
                n=16_384,
                r=8,
                p=1,
                dklen=32,
            )
            if answer
            else b""
        )
        metadata = self.store.metadata()
        account = UserAccount(
            account_id=f"account-{uuid.uuid4().hex}",
            username=username,
            role=role,
            created_at=_timestamp(),
            device_id=str(metadata["device_id"]),
            sync_scope="local-first",
            password_salt=salt.hex(),
            password_hash=password_hash.hex(),
            security_question=question,
            security_answer_salt=answer_salt.hex(),
            security_answer_hash=answer_hash.hex(),
        )
        self.store.add_account(account)
        return account

    def reset_password(
        self, username: str, security_answer: str, new_password: str
    ) -> UserAccount | None:
        """Reset a password only when the stored recovery answer verifies."""
        if type(security_answer) is not str or type(new_password) is not str:
            return None
        if not 10 <= len(new_password) <= 256:
            return None
        for account in self.store.accounts():
            if account.username.casefold() != username.casefold():
                continue
            if not account.security_answer_hash or not account.security_answer_salt:
                return None
            try:
                actual = hashlib.scrypt(
                    security_answer.strip().casefold().encode("utf-8"),
                    salt=bytes.fromhex(account.security_answer_salt),
                    n=16_384,
                    r=8,
                    p=1,
                    dklen=32,
                ).hex()
            except (TypeError, ValueError):
                return None
            if not secrets.compare_digest(actual, account.security_answer_hash):
                return None
            salt = secrets.token_bytes(16)
            updated = replace(
                account,
                password_salt=salt.hex(),
                password_hash=hashlib.scrypt(
                    new_password.encode("utf-8"),
                    salt=salt,
                    n=16_384,
                    r=8,
                    p=1,
                    dklen=32,
                ).hex(),
            )
            self.store.update_account(updated)
            return updated
        return None

    def authenticate(self, username: str, password: str) -> UserAccount | None:
        for account in self.store.accounts():
            if account.username.casefold() != username.casefold():
                continue
            try:
                actual = hashlib.scrypt(
                    password.encode("utf-8"),
                    salt=bytes.fromhex(account.password_salt),
                    n=16_384,
                    r=8,
                    p=1,
                    dklen=32,
                ).hex()
            except (TypeError, ValueError):
                return None
            return account if secrets.compare_digest(actual, account.password_hash) else None
        return None

    def sync_manifest(self) -> dict[str, object]:
        """Describe future sync identity without exporting credentials or content."""
        metadata = self.store.metadata()
        return {
            "schema_version": 1,
            "device_id": metadata.get("device_id"),
            "accounts": [account.to_public_dict() for account in self.store.accounts()],
            "sync_scope": "local-first; explicit future pairing required",
        }


class LearningScheduler:
    """Daemon scheduler; shutting down the controller always stops it."""

    def __init__(self, service: LearningService, *, interval_s: int = 900) -> None:
        self.service = service
        self.interval_s = max(60, interval_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="acb-learning", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.run_due()
            except (OSError, RuntimeError, ValueError):
                # Learning must never stop task execution or leak feed details.
                pass
            self._stop.wait(self.interval_s)


def _fetch_feed(source: ResearchSource) -> bytes:
    _validate_research_url(source.url)
    request = urllib.request.Request(
        source.url,
        headers={"User-Agent": "ACB-Learning/1.0", "Accept": "application/rss+xml, application/xml"},
        method="GET",
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    with opener.open(request, timeout=10) as response:  # nosec B310 - HTTPS allowlist above
        return cast(bytes, response.read(MAX_FEED_BYTES + 1))


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        new_url: str,
    ) -> urllib.request.Request | None:
        _validate_research_url(new_url)
        return super().redirect_request(request, fp, code, msg, headers, new_url)


def _validate_research_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise ValueError("research source is not trusted")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("research source port is invalid") from error
    if port not in {None, 443}:
        raise ValueError("research source port is not allowed")
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("research source host is missing")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("research source host could not be resolved") from error
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError("research source resolves to a non-public address")


def _parse_feed(payload: bytes, source: ResearchSource) -> tuple[KnowledgeItem, ...]:
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("research feed is too large")
    # ElementTree is safe for the bounded RSS subset once DTDs/entities are
    # rejected before parsing; feeds never need those XML features.
    lowered = payload.upper()
    if b"<!DOCTYPE" in lowered or b"<!ENTITY" in lowered or b"<![" in lowered:
        raise ValueError("research feed contains forbidden XML declarations")
    if payload.count(b"<") > 4_096:
        raise ValueError("research feed has too many XML nodes")
    root = ET.fromstring(payload)  # nosec B314 - bounded RSS subset, declarations rejected above
    records = list(root.iter())
    entries = [item for item in records if _local_name(item.tag) in {"item", "entry"}]
    result: list[KnowledgeItem] = []
    for entry in entries[:MAX_ITEMS_PER_FEED]:
        title = _child_text(entry, {"title"})
        link = _child_text(entry, {"link", "guid"})
        if not link:
            for child in entry:
                if _local_name(child.tag) == "link" and child.attrib.get("href"):
                    link = child.attrib["href"]
                    break
        if not title or not link:
            continue
        parsed = urllib.parse.urlsplit(link.strip())
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
            continue
        summary = _child_text(entry, {"description", "summary", "content"})[:1200]
        published = _child_text(entry, {"pubDate", "published", "updated"}) or None
        item_id = "knowledge-" + hashlib.sha256(link.encode("utf-8")).hexdigest()[:32]
        result.append(
            KnowledgeItem(
                item_id=item_id,
                title=title[:240],
                url=link[:2048],
                source=source.name,
                topic=source.topic,
                summary=summary,
                published_at=published,
                discovered_at=_timestamp(),
                trust="allow-listed-feed",
            )
        )
    return tuple(result)


def _child_text(element: ET.Element, names: set[str]) -> str:
    for child in element:
        if _local_name(child.tag) in names and child.text:
            return " ".join(child.text.split())
    return ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or (metadata.st_mode & 0o777) != 0o700:
        raise RuntimeError("learning state directory is not owner-controlled")


def _validate_file_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or (metadata.st_mode & 0o777) != 0o600:
        raise RuntimeError("learning state file is not owner-controlled")


def _bounded_limit(value: int, maximum: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("learning limit is invalid")
    return min(value, maximum)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _learning_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", value.casefold())
        if token not in {"the", "and", "oder", "und", "für", "mit", "von"}
    }


def _feedback_hint(comment: str) -> str | None:
    lowered = comment.casefold()
    if any(token in lowered for token in ("zu knapp", "mehr detail", "ausführlicher", "ausfuehrlicher")):
        return "more-detail"
    if any(token in lowered for token in ("zu kompliziert", "einfacher", "verständlicher", "verstaendlicher")):
        return "simple-language"
    if any(token in lowered for token in ("zu lang", "kürzer", "kuerzer")):
        return "concise"
    if any(token in lowered for token in ("technischer", "code", "präziser", "praeziser")):
        return "technical"
    return None


def asdict_account(account: UserAccount) -> dict[str, object]:
    return {
        "account_id": account.account_id,
        "username": account.username,
        "role": account.role,
        "created_at": account.created_at,
        "device_id": account.device_id,
        "sync_scope": account.sync_scope,
        "password_salt": account.password_salt,
        "password_hash": account.password_hash,
        "security_question": account.security_question,
        "security_answer_salt": account.security_answer_salt,
        "security_answer_hash": account.security_answer_hash,
    }


def _knowledge_from_dict(value: dict[str, object]) -> KnowledgeItem:
    published_value = value.get("published_at")
    return KnowledgeItem(
        item_id=str(value["item_id"]), title=str(value["title"]), url=str(value["url"]),
        source=str(value["source"]), topic=str(value["topic"]), summary=str(value.get("summary", "")),
        published_at=published_value if isinstance(published_value, str) else None,
        discovered_at=str(value["discovered_at"]), trust=str(value.get("trust", "unknown")),
    )


def _experience_from_dict(value: dict[str, object]) -> ExperienceItem:
    raw_criteria = value.get("failed_criteria", [])
    criteria = raw_criteria if isinstance(raw_criteria, (list, tuple)) else []
    return ExperienceItem(
        experience_id=str(value["experience_id"]),
        goal=str(value["goal"]),
        outcome=str(value["outcome"]),
        lesson=str(value["lesson"]),
        failed_criteria=tuple(
            str(item) for item in criteria if isinstance(item, str)
        )[:8],
        created_at=str(value["created_at"]),
    )


def _suggestion_from_dict(value: dict[str, object]) -> ImprovementSuggestion:
    raw_sources = value.get("source_ids", [])
    sources = raw_sources if isinstance(raw_sources, (list, tuple)) else []
    return ImprovementSuggestion(
        suggestion_id=str(value["suggestion_id"]), kind=str(value["kind"]), title=str(value["title"]),
        description=str(value["description"]), source_ids=tuple(str(item) for item in sources if isinstance(item, str)),
        status=str(value["status"]), priority=str(value["priority"]), auto_apply=bool(value["auto_apply"]),
        release_gate_required=bool(value["release_gate_required"]), created_at=str(value["created_at"]), updated_at=str(value["updated_at"]),
    )


def _update_from_dict(value: dict[str, object]) -> SelfUpdateProposal:
    raw_sources = value.get("source_ids", [])
    sources = raw_sources if isinstance(raw_sources, (list, tuple)) else []
    raw_evidence = value.get("verification_evidence", [])
    evidence = raw_evidence if isinstance(raw_evidence, (list, tuple)) else []
    return SelfUpdateProposal(
        update_id=str(value["update_id"]), title=str(value["title"]), reason=str(value["reason"]),
        source_ids=tuple(str(item) for item in sources if isinstance(item, str)), status=str(value["status"]),
        gate_status=str(value["gate_status"]), created_at=str(value["created_at"]), updated_at=str(value["updated_at"]),
        verification_evidence=tuple(
            str(item)[:MAX_GATE_EVIDENCE_TEXT]
            for item in evidence
            if isinstance(item, str) and item.strip()
        )[:MAX_GATE_EVIDENCE],
    )


def _gate_evidence_complete(evidence: Sequence[str]) -> bool:
    required = ("tests", "security", "release", "rollback")
    return all(
        any(
            item.casefold().startswith(f"{name}:")
            and item.casefold().endswith(":passed")
            for item in evidence
        )
        for name in required
    )


def _account_from_dict(value: dict[str, object]) -> UserAccount:
    return UserAccount(
        account_id=str(value["account_id"]), username=str(value["username"]), role=str(value["role"]),
        created_at=str(value["created_at"]), device_id=str(value["device_id"]), sync_scope=str(value["sync_scope"]),
        password_salt=str(value["password_salt"]), password_hash=str(value["password_hash"]),
        security_question=str(value.get("security_question", "")),
        security_answer_salt=str(value.get("security_answer_salt", "")),
        security_answer_hash=str(value.get("security_answer_hash", "")),
    )


__all__ = [
    "RESEARCH_INTERVAL_S",
    "RESEARCH_SOURCES",
    "ExperienceItem",
    "ImprovementSuggestion",
    "KnowledgeItem",
    "LearningScheduler",
    "LearningService",
    "LearningStatus",
    "LearningStore",
    "ResearchSource",
    "SelfUpdateProposal",
    "UserAccount",
]
