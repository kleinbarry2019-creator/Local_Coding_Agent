"""Bounded local Ollama tool-calling for complex autonomous goals.

The model is never trusted with shell text or host paths.  It may request only
the three existing project tools; every request is validated before the caller
executes it and completion still requires independent runtime evidence.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
_MAX_RESPONSE_BYTES = 65_536
_MAX_MESSAGE_BYTES = 65_536
_MAX_TURNS = 12
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,127}$")
_ALLOWED_TOOLS = frozenset(
    {"project.read-file", "project.write-file", "project.run-process"}
)


class LocalModelError(RuntimeError):
    """A bounded, user-safe local model failure."""


@dataclass(frozen=True)
class ModelAction:
    kind: str
    name: str | None = None
    arguments: Mapping[str, object] | None = None
    summary: str | None = None


@dataclass(frozen=True)
class ModelRun:
    model: str
    completed: bool
    verified: bool
    actions: tuple[Mapping[str, object], ...]
    summary: str
    turns: int


class LocalOllamaClient:
    """Use only a loopback Ollama endpoint with strict size limits."""

    def __init__(self, endpoint: str | None = None, model: str | None = None) -> None:
        raw_endpoint = endpoint or os.environ.get("ACB_OLLAMA_URL", _DEFAULT_ENDPOINT)
        parsed = urllib.parse.urlparse(raw_endpoint)
        if parsed.scheme != "http" or parsed.hostname is None:
            raise LocalModelError("local model endpoint must be HTTP loopback")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise LocalModelError("local model endpoint must be an IP loopback") from error
        if not address.is_loopback:
            raise LocalModelError("local model endpoint must be loopback")
        self.endpoint = raw_endpoint.rstrip("/")
        selected = model or os.environ.get("ACB_MODEL")
        if selected is not None and not _MODEL_PATTERN.fullmatch(selected):
            raise LocalModelError("local model name is invalid")
        self._model = selected

    def model_name(self) -> str:
        if self._model is not None:
            return self._model
        payload = self._get_json("/api/tags")
        raw_models = payload.get("models") if isinstance(payload, Mapping) else None
        if not isinstance(raw_models, list):
            raise LocalModelError("local model inventory is invalid")
        candidates = [
            item
            for item in raw_models
            if isinstance(item, Mapping)
            and isinstance(item.get("name"), str)
            and _MODEL_PATTERN.fullmatch(str(item["name"]))
        ]
        if not candidates:
            raise LocalModelError("no local Ollama model is available")
        coder_candidates = [
            item for item in candidates if "coder" in str(item["name"]).casefold()
        ] or candidates
        available = _available_memory_bytes()
        fitting = [
            item
            for item in coder_candidates
            if not isinstance(item.get("size"), int)
            or item["size"] <= int(available * 0.88)
        ]
        if not fitting:
            fitting = coder_candidates
        preferred_item = max(
            fitting,
            key=lambda item: (
                int(item.get("size", 0)) if isinstance(item.get("size"), int) else 0,
                str(item["name"]),
            ),
        )
        preferred = str(preferred_item["name"])
        self._model = preferred
        return preferred

    def chat(self, messages: Sequence[Mapping[str, str]]) -> str:
        encoded = json.dumps(list(messages), ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_MESSAGE_BYTES:
            raise LocalModelError("local model context exceeds the bounded limit")
        payload = {
            "model": self.model_name(),
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": 0},
        }
        request = urllib.request.Request(
            self.endpoint + "/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise LocalModelError("local Ollama request failed") from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LocalModelError("local model response exceeds the bounded limit")
        try:
            body = json.loads(raw.decode("utf-8"))
            message = body["message"]
            content = message["content"]
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise LocalModelError("local Ollama response is invalid") from error
        if not isinstance(content, str) or not content.strip():
            raise LocalModelError("local Ollama response has no text")
        return content.strip()

    def _get_json(self, path: str) -> object:
        try:
            with urllib.request.urlopen(self.endpoint + path, timeout=5) as response:  # nosec B310
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise LocalModelError("local Ollama inventory request failed") from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LocalModelError("local Ollama inventory is too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise LocalModelError("local Ollama inventory is invalid") from error


class LocalModelRunner:
    """Drive bounded tool calls and require an observed verification command."""

    def __init__(self, client: LocalOllamaClient | None = None) -> None:
        self.client = LocalOllamaClient() if client is None else client

    def run(
        self,
        goal: str,
        dispatch: Callable[[str, Mapping[str, object]], Mapping[str, object]],
    ) -> ModelRun:
        model = self.client.model_name()
        messages: list[Mapping[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are ACB's bounded local coding executor. Return exactly one JSON object. "
                    "Allowed tool actions are project.read-file, project.write-file, and "
                    "project.run-process. Paths are project-relative; run-process argv is an "
                    "array and never a shell. Inspect before editing, implement the user's goal, "
                    "run a meaningful verification command, and if it fails inspect the files and "
                    "make a concrete correction; never repeat an identical failed action. Then "
                    "return {\"action\":\"done\",\"summary\":\"...\"}. Never claim done without "
                    "observed verification."
                ),
            },
            {"role": "user", "content": f"GOAL:\n{goal}"},
        ]
        actions: list[Mapping[str, object]] = []
        fingerprints: set[str] = set()
        verified = False
        for turn in range(1, _MAX_TURNS + 1):
            response = self.client.chat(messages)
            action = _parse_action(response)
            messages.append({"role": "assistant", "content": response})
            if action.kind == "done":
                if not actions or not verified:
                    return ModelRun(model, False, verified, tuple(actions), action.summary or "", turn)
                return ModelRun(model, True, True, tuple(actions), action.summary or "", turn)
            if action.name not in _ALLOWED_TOOLS or action.arguments is None:
                raise LocalModelError("local model requested a forbidden tool")
            result = dict(dispatch(action.name, action.arguments))
            actions.append({"tool": action.name, "result": result})
            fingerprint = json.dumps(
                {"tool": action.name, "arguments": action.arguments, "result": result},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            if fingerprint in fingerprints:
                return ModelRun(
                    model,
                    False,
                    verified,
                    tuple(actions),
                    "model loop detected: identical tool action repeated",
                    turn,
                )
            fingerprints.add(fingerprint)
            data = result.get("data")
            if result.get("success") is True and isinstance(data, Mapping):
                verified = verified or bool(result.get("verified"))
            serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            messages.append({"role": "tool", "content": serialized[:_MAX_RESPONSE_BYTES]})
        return ModelRun(model, False, verified, tuple(actions), "model turn budget exhausted", _MAX_TURNS)


def _parse_action(raw: str) -> ModelAction:
    value = raw.strip()
    blocks = re.findall(r"```(?:json)?\s*\n?(.*?)```", value, flags=re.IGNORECASE | re.DOTALL)
    if blocks:
        # A few instruct models emit a queue of fenced actions in one reply.
        # Consume exactly the first validated object; subsequent turns remain
        # authoritative because every action is independently dispatched.
        value = blocks[0].strip()
    try:
        document = json.loads(value)
    except (ValueError, TypeError) as error:
        raise LocalModelError("local model action is not JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("action"), str):
        raise LocalModelError("local model action schema is invalid")
    kind = document["action"]
    if kind == "done":
        if set(document) != {"action", "summary"} or not isinstance(document["summary"], str):
            raise LocalModelError("local model completion schema is invalid")
        return ModelAction(kind="done", summary=document["summary"][:2000])
    if kind in _ALLOWED_TOOLS:
        # Some local instruct models emit the tool name in `action` and flatten
        # the arguments. Accept only this documented compatibility shape.
        if set(document) - {"action", "arguments", "path", "file_path", "content", "argv", "cwd"}:
            raise LocalModelError("local model tool schema is invalid")
        if "arguments" in document:
            if set(document) != {"action", "arguments"} or not isinstance(
                document["arguments"], dict
            ):
                raise LocalModelError("local model tool arguments are invalid")
            arguments = dict(document["arguments"])
        else:
            arguments = {
                key: document[key]
                for key in ("path", "content", "argv", "cwd")
                if key in document
            }
            if "file_path" in document:
                if "path" in arguments:
                    raise LocalModelError("local model tool path is ambiguous")
                arguments["path"] = document["file_path"]
        name = kind
    elif kind == "tool" and set(document) == {"action", "name", "arguments"}:
        if not isinstance(document["name"], str) or not isinstance(document["arguments"], dict):
            raise LocalModelError("local model tool arguments are invalid")
        name = document["name"]
        arguments = dict(document["arguments"])
    else:
        raise LocalModelError("local model tool schema is invalid")
    if name not in _ALLOWED_TOOLS:
        raise LocalModelError("local model requested a forbidden tool")
    if name == "project.run-process":
        argv = arguments.get("argv")
        if isinstance(argv, list) and argv and argv[0] in {"sh", "bash", "zsh", "fish"}:
            raise LocalModelError("local model may not request a shell executable")
    return ModelAction(kind="tool", name=name, arguments=arguments)


def _available_memory_bytes() -> int:
    """Return a conservative host-memory budget for model selection."""
    try:
        with Path("/proc/meminfo").open(encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


__all__ = ["LocalModelError", "LocalModelRunner", "LocalOllamaClient", "ModelRun"]
