import json
from pathlib import Path

import safe_agent_v43 as agent


ROOT = agent.ROOT
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
        f"{label}: Angriff wurde akzeptiert"
    )


def test_schema():
    agent.validate_state({
        "version": 43,
        "tasks_completed": 0,
        "last_result": None,
    })

    expect_blocked(
        "Negative Tasks",
        lambda: agent.validate_state({
            "version": 43,
            "tasks_completed": -1,
            "last_result": None,
        }),
    )


def test_tool_allowlist():
    expect_blocked(
        "Unknown Tool",
        lambda: agent.execute_tool(
            "shell",
            {},
        ),
    )


def test_path_escape():
    for path in [
        "../escape.txt",
        "../../escape.txt",
        "/etc/shadow",
    ]:
        expect_blocked(
            f"Path Escape {path}",
            lambda p=path: agent.write_file(
                p,
                "NOPE",
            ),
        )


def test_write_read():
    target = "v43_rw.txt"

    agent.write_file(
        target,
        "V43_OK",
    )

    assert (
        agent.read_file(target)
        == "V43_OK"
    )

    (RUNTIME / target).unlink()


def test_content_limit():
    expect_blocked(
        "Content Limit",
        lambda: agent.write_file(
            "too_big.txt",
            "X" * (
                agent.MAX_CONTENT_BYTES + 1
            ),
        ),
    )


def test_argument_limit():
    script = RUNTIME / "arg_test.py"

    agent.write_file(
        "arg_test.py",
        "print('ARGS_OK')\n",
    )

    expect_blocked(
        "Too Many Args",
        lambda: agent.run_python(
            "arg_test.py",
            ["x"] * (
                agent.MAX_SCRIPT_ARGS + 1
            ),
        ),
    )

    script.unlink()


def test_python_execution():
    agent.write_file(
        "exec_test.py",
        "print('V43_EXEC_OK')\n",
    )

    result = agent.run_python(
        "exec_test.py",
    )

    assert result["exit_code"] == 0
    assert "V43_EXEC_OK" in result["stdout"]

    (RUNTIME / "exec_test.py").unlink()


def test_python_escape():
    agent.write_file(
        "escape_probe.py",
        (
            "from pathlib import Path\n"
            "print(Path('/var/home/mklein').exists())\n"
            "print(Path('/etc/shadow').exists())\n"
            "print(Path('/sys').exists())\n"
        ),
    )

    result = agent.run_python(
        "escape_probe.py",
    )

    assert result["exit_code"] == 0
    assert result["stdout"].splitlines() == [
        "False",
        "False",
        "False",
    ]

    (RUNTIME / "escape_probe.py").unlink()




def test_parser():
    direct = json.dumps({
        "action": "done",
        "result": "OK",
    })

    parsed = agent.parse_action(
        direct
    )

    assert parsed["action"] == "done"


    fenced = (
        "```json\n"
        '{"action":"done","result":"FENCED_OK"}'
        "\n```"
    )

    parsed = agent.parse_action(
        fenced
    )

    assert parsed["result"] == "FENCED_OK"


    shorthand = json.dumps({
        "action": "write_file",
        "arguments": {
            "path": "x.txt",
            "content": "X",
        },
    })

    parsed = agent.parse_action(
        shorthand
    )

    assert parsed == {
        "action": "tool",
        "name": "write_file",
        "arguments": {
            "path": "x.txt",
            "content": "X",
        },
    }


    expect_blocked(
        "Multiple JSON Objects",
        lambda: agent.parse_action(
            '{"action":"done","result":"A"}\n'
            '{"action":"done","result":"B"}'
        ),
    )


    expect_blocked(
        "JSON plus prose",
        lambda: agent.parse_action(
            'Hier ist die Antwort:\n'
            '{"action":"done","result":"BAD"}'
        ),
    )


    expect_blocked(
        "Unknown shorthand action",
        lambda: agent.parse_action(
            '{"action":"shell","arguments":{}}'
        ),
    )


    expect_blocked(
        "Two fenced blocks",
        lambda: agent.parse_action(
            '```json\n'
            '{"action":"done","result":"A"}\n'
            '```\n'
            '```json\n'
            '{"action":"done","result":"B"}\n'
            '```'
        ),
    )


def test_audit_chain():
    agent.audit(
        "TEST_EVENT",
        {
            "ok": True,
        },
    )

    assert agent.verify_audit_chain()


def test_agent_code_present():
    assert (
        ROOT / "safe_agent_v43.py"
    ).is_file()


if __name__ == "__main__":
    tests = [
        test_schema,
        test_tool_allowlist,
        test_path_escape,
        test_write_read,
        test_content_limit,
        test_argument_limit,
        test_python_execution,
        test_python_escape,
        test_parser,
        test_audit_chain,
        test_agent_code_present,
    ]

    for test in tests:
        test()

    print()
    print("V43 TESTS OK")
