from __future__ import annotations

from collections.abc import Mapping

import pytest

from autonomous_agent.core.local_model import (
    LocalModelError,
    LocalModelRunner,
    ModelRun,
    _parse_action,
)


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)

    def model_name(self) -> str:
        return "qwen2.5-coder:7b"

    def chat(self, _messages: object) -> str:
        return next(self.responses)


def test_model_runner_requires_observed_verification() -> None:
    runner = LocalModelRunner(
        _FakeClient(
            [
                '{"action":"tool","name":"project.write-file","arguments":{"path":"app.py","content":"print(42)"}}',
                '{"action":"done","summary":"written"}',
            ]
        )
    )
    seen: list[str] = []

    def dispatch(name: str, _arguments: Mapping[str, object]) -> Mapping[str, object]:
        seen.append(name)
        return {"success": True, "data": {"written": True}}

    result = runner.run("create app", dispatch)

    assert result.completed is False
    assert result.verified is False
    assert seen == ["project.write-file"]


def test_model_runner_completes_after_real_verified_tool_result() -> None:
    runner = LocalModelRunner(
        _FakeClient(
            [
                '{"action":"tool","name":"project.run-process","arguments":{"argv":["python3","-c","print(42)"],"cwd":"."}}',
                '{"action":"done","summary":"verified"}',
            ]
        )
    )

    result = runner.run(
        "run a verification", lambda _name, _arguments: {"success": True, "verified": True, "data": {"exit_code": 0}}
    )

    assert isinstance(result, ModelRun)
    assert result.completed is True
    assert result.verified is True


def test_model_parser_rejects_shell_and_extra_fields() -> None:
    with pytest.raises(LocalModelError):
        _parse_action(
            '{"action":"tool","name":"project.run-process","arguments":{"argv":["sh","-c","echo ok"]}}'
        )
    with pytest.raises(LocalModelError):
        _parse_action('{"action":"done","summary":"ok","verified":true}')


def test_model_parser_accepts_bounded_fenced_tool_queue_one_action_at_a_time() -> None:
    action = _parse_action(
        """```json
        {"action":"project.write-file","path":"hello.py","content":"print(42)"}
        ```
        ```json
        {"action":"done","summary":"later"}
        ```"""
    )

    assert action.kind == "tool"
    assert action.name == "project.write-file"
    assert action.arguments == {
        "path": "hello.py",
        "content": "print(42)",
    }
