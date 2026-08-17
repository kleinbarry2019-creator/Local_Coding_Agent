import json

import safe_agent_v44 as agent


RUNTIME = agent.RUNTIME


def expect_blocked(label, fn):
    print("[TEST]", label)

    try:
        fn()
    except Exception as exc:
        print(
            "       BLOCKED:",
            type(exc).__name__,
        )
        return

    raise AssertionError(
        f"{label}: nicht blockiert"
    )


def test_policy():
    policy = agent.build_mission_policy()

    assert set(policy) == {
        "list_files",
        "read_file",
        "write_file",
        "run_python",
    }


def test_readonly_policy():
    import os

    old = os.environ.get(
        "SAFE_AGENT_POLICY"
    )

    os.environ["SAFE_AGENT_POLICY"] = "readonly"

    try:
        policy = agent.build_mission_policy()

        assert policy["read_file"] is True
        assert policy["write_file"] is False
        assert policy["run_python"] is False

    finally:
        if old is None:
            os.environ.pop(
                "SAFE_AGENT_POLICY",
                None,
            )
        else:
            os.environ["SAFE_AGENT_POLICY"] = old


def test_tool_allowlist():
    budget = agent.MissionBudget()
    policy = agent.DEFAULT_POLICY

    expect_blocked(
        "Unknown tool",
        lambda: agent.execute_tool(
            "shell",
            {},
            policy,
            budget,
        ),
    )


def test_path_policy():
    for path in [
        "../escape.txt",
        "../../escape.txt",
        "/etc/shadow",
        "/var/home/mklein/x",
    ]:
        expect_blocked(
            f"Path {path}",
            lambda p=path: agent.write_file(
                p,
                "X",
            ),
        )


def test_workspace_prefix():
    target = "v44_prefix.txt"

    agent.write_file(
        "/workspace/" + target,
        "PREFIX_OK",
    )

    assert agent.read_file(
        target
    ) == "PREFIX_OK"

    (
        RUNTIME / target
    ).unlink()


def test_budget():
    budget = agent.MissionBudget()

    budget.consume(
        "write_file"
    )

    assert budget.total == 1
    assert budget.writes == 1


def test_policy_blocks_write():
    budget = agent.MissionBudget()

    expect_blocked(
        "Readonly write",
        lambda: agent.execute_tool(
            "write_file",
            {
                "path": "x.txt",
                "content": "X",
            },
            agent.READ_ONLY_POLICY,
            budget,
        ),
    )


def test_policy_blocks_exec():
    budget = agent.MissionBudget()

    expect_blocked(
        "Noexec run_python",
        lambda: agent.execute_tool(
            "run_python",
            {
                "script": "x.py",
            },
            agent.NO_EXEC_POLICY,
            budget,
        ),
    )


def test_limits():
    budget = agent.MissionBudget()

    budget.total = agent.MAX_TOOL_CALLS

    expect_blocked(
        "Total budget",
        lambda: budget.check(
            "read_file"
        ),
    )


def test_parser_formats():
    direct = json.dumps({
        "action": "done",
        "result": "OK",
    })

    assert (
        agent.parse_action(direct)["action"]
        == "done"
    )

    fenced = (
        "```json\n"
        '{"action":"done","result":"FENCED"}'
        "\n```"
    )

    assert (
        agent.parse_action(
            fenced
        )["result"]
        == "FENCED"
    )

    shorthand = json.dumps({
        "action": "write_file",
        "arguments": {
            "path": "a.txt",
            "content": "A",
        },
    })

    parsed = agent.parse_action(
        shorthand
    )

    assert parsed["name"] == "write_file"


def test_parser_rejects_prose():
    expect_blocked(
        "Prose",
        lambda: agent.parse_action(
            "Hier:\n"
            '{"action":"done","result":"BAD"}'
        ),
    )


def test_audit_chain():
    agent.audit(
        "V44_TEST",
        {"ok": True},
    )

    assert (
        agent.verify_audit_chain()
    )


if __name__ == "__main__":
    tests = [
        test_policy,
        test_readonly_policy,
        test_tool_allowlist,
        test_path_policy,
        test_workspace_prefix,
        test_budget,
        test_policy_blocks_write,
        test_policy_blocks_exec,
        test_limits,
        test_parser_formats,
        test_parser_rejects_prose,
        test_audit_chain,
    ]

    for test in tests:
        test()

    print()
    print("V44 TESTS OK")
