from pathlib import Path
import subprocess
import safe_agent_v16 as agent


WORKSPACE = Path.cwd().resolve()


def ok(name):
    print("[PASS]", name)


def blocked(name, fn):
    print("[TEST]", name)

    try:
        fn()
    except Exception as exc:
        print("       BLOCKED:", type(exc).__name__)
        ok(name)
        return

    raise AssertionError(
        "Angriff wurde nicht blockiert"
    )


def test_unknown_tool():
    blocked(
        "Unknown Tool",
        lambda: agent.execute_tool(
            "exec",
            {"cmd": "rm -rf /"},
        ),
    )


def test_script_allowlist():
    blocked(
        "Script Allowlist",
        lambda: agent.execute_tool(
            "run_python_sandbox",
            {
                "script": "sandbox_v17_test.py"
            },
        ),
    )


def test_safe_script():
    print("[TEST] Safe Script")

    result = agent.execute_tool(
        "run_python_sandbox",
        {
            "script": "safe_test.py"
        },
    )

    assert result["returncode"] == 0
    assert "SAFE TEST OK" in result["stdout"]

    ok("Safe Script")


def test_bwrap_exists():
    print("[TEST] bubblewrap")

    result = subprocess.run(
        ["bwrap", "--version"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0

    ok(result.stdout.strip())


def test_host_escape():

    print("[TEST] Host Escape")

    result = agent.run_python_sandbox(
        "safe_test.py"
    )

    assert result["returncode"] == 0

    for name in [
        "ESCAPE_TEST",
    ]:
        assert not Path("/tmp/" + name).exists()
        assert not Path("/var/" + name).exists()

    ok("Host Escape")


if __name__ == "__main__":

    tests = [
        test_unknown_tool,
        test_script_allowlist,
        test_safe_script,
        test_bwrap_exists,
        test_host_escape,
    ]

    passed = 0

    for test in tests:
        test()
        passed += 1

    print()
    print(
        f"{passed}/{len(tests)} V17 SECURITY TESTS PASSED"
    )
    print(
        "V17 SECURITY HARNESS OK"
    )
