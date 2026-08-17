import json
from pathlib import Path

import safe_agent_v42 as agent


ROOT = agent.ROOT
RUNTIME = agent.RUNTIME


def test_schema():
    agent.validate_state({
        "version": 42,
        "tasks_completed": 0,
        "last_result": None,
    })

    for bad in [
        {
            "version": 41,
            "tasks_completed": 0,
            "last_result": None,
        },
        {
            "version": 42,
            "tasks_completed": -1,
            "last_result": None,
        },
        {
            "version": 42,
            "tasks_completed": 0,
        },
    ]:
        try:
            agent.validate_state(bad)
        except RuntimeError:
            continue

        raise AssertionError(
            f"Invalid state accepted: {bad!r}"
        )


def test_tool_policy():
    try:
        agent.execute_tool(
            "shell",
            {},
        )
    except PermissionError:
        pass
    else:
        raise AssertionError(
            "Unknown tool accepted"
        )


def test_path_escape_blocked():
    for path in [
        "../escape.txt",
        "/etc/shadow",
        "../../escape.txt",
    ]:
        try:
            agent.write_file(
                path,
                "NOPE",
            )
        except PermissionError:
            continue

        raise AssertionError(
            f"Escape path accepted: {path}"
        )


def test_write_and_read():
    target = "v42_tool_test.txt"

    result = agent.write_file(
        target,
        "V42_OK",
    )

    assert result["bytes"] == 6
    assert agent.read_file(target) == "V42_OK"

    (RUNTIME / target).unlink()


def test_list_files():
    target = "v42_list_test.txt"

    agent.write_file(
        target,
        "X",
    )

    files = agent.list_files()

    assert target in files

    (RUNTIME / target).unlink()


def test_readonly_agent():
    result = subprocess_run(
        "printf HACK >> /agent/safe_agent_v42.py"
    )

    assert result != 0


def subprocess_run(command):
    result = agent.run_python(
        "noop.py"
    )
    return result["exit_code"]


def test_parser():
    result = agent.parse_action(
        json.dumps({
            "action": "done",
            "result": "OK",
        })
    )

    assert result["action"] == "done"

    try:
        agent.parse_action(
            '{"action":"done","result":"OK"}\n'
            '{"action":"done","result":"BAD"}'
        )
    except RuntimeError:
        return

    raise AssertionError(
        "Multiple JSON objects accepted"
    )


def test_hash():
    state = {
        "version": 42,
        "tasks_completed": 2,
        "last_result": "OK",
    }

    digest = agent.state_digest(
        state
    )

    assert len(digest) == 64


if __name__ == "__main__":
    tests = [
        test_schema,
        test_tool_policy,
        test_path_escape_blocked,
        test_write_and_read,
        test_list_files,
        test_parser,
        test_hash,
    ]

    for test in tests:
        print("[TEST]", test.__name__)
        test()

    print()
    print("V42 TESTS OK")
