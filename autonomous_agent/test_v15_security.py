from pathlib import Path
import safe_agent_v15 as agent


WORKSPACE = Path.cwd().resolve()


def expect_block(label, func):
    print(f"[TEST] {label}")

    try:
        func()
    except (PermissionError, TypeError, FileNotFoundError):
        print("       BLOCKED")
        return

    raise AssertionError(
        "Angriff wurde NICHT blockiert"
    )


def test_unknown_tool():
    expect_block(
        "Unbekanntes Tool",
        lambda: agent.execute_tool(
            "exec",
            {"command": "rm -rf /"},
        ),
    )


def test_unapproved_script():
    expect_block(
        "Nicht freigegebenes Skript",
        lambda: agent.execute_tool(
            "run_python",
            {"script": "safe_agent_v15.py"},
        ),
    )


def test_traversal():
    expect_block(
        "Path Traversal",
        lambda: agent.execute_tool(
            "run_python",
            {"script": "../safe_test.py"},
        ),
    )


def test_absolute_path():
    expect_block(
        "Absoluter Pfad",
        lambda: agent.execute_tool(
            "run_python",
            {"script": str(WORKSPACE.parent / "safe_test.py")},
        ),
    )


def test_shell_string_is_not_accepted_as_tool():
    expect_block(
        "Shell-Befehl als Script",
        lambda: agent.execute_tool(
            "run_python",
            {
                "script": "bash -c 'echo PWNED'",
            },
        ),
    )


def test_args_must_be_strings():
    expect_block(
        "Nicht-String Argument",
        lambda: agent.execute_tool(
            "run_python",
            {
                "script": "safe_test.py",
                "args": ["ok", 123],
            },
        ),
    )


def test_safe_execution():
    print("[TEST] Sichere Python-Ausführung")

    result = agent.execute_tool(
        "run_python",
        {
            "script": "safe_test.py",
        },
    )

    assert result["returncode"] == 0
    assert "SAFE TEST OK" in result["stdout"]

    print("       PASS")


if __name__ == "__main__":
    tests = [
        test_unknown_tool,
        test_unapproved_script,
        test_traversal,
        test_absolute_path,
        test_shell_string_is_not_accepted_as_tool,
        test_args_must_be_strings,
        test_safe_execution,
    ]

    passed = 0

    for test in tests:
        test()
        passed += 1

    print()
    print(f"{passed}/{len(tests)} V15 SECURITY TESTS PASSED")
    print("V15 SECURITY HARNESS OK")
