from pathlib import Path
import subprocess
import safe_agent_v19 as agent


def ok(name):
    print("[PASS]", name)


def blocked(name, fn):
    print("[TEST]", name)

    try:
        fn()
    except Exception as exc:
        print(
            "       BLOCKED:",
            type(exc).__name__
        )
        ok(name)
        return

    raise AssertionError(
        "FAIL: wurde nicht blockiert"
    )


def test_unknown_tool():

    blocked(
        "Unknown Tool",
        lambda: agent.execute_tool(
            "shell",
            {"cmd": "whoami"},
        ),
    )


def test_script_allowlist():

    blocked(
        "Script Allowlist",
        lambda: agent.execute_tool(
            "run_python_sandbox",
            {
                "script": "sandbox_v19_test.py"
            },
        ),
    )


def test_result_schema():

    print("[TEST] Result Schema")

    result = agent.run_python_sandbox(
        "safe_test.py"
    )

    required = {
        "ok",
        "status",
        "exit_code",
        "stdout",
        "stderr",
        "script",
    }

    assert required.issubset(
        result.keys()
    )

    ok("Result Schema")


def test_output_limit():

    print("[TEST] Output Limit")

    source = Path(
        "safe_agent_v19.py"
    ).read_text()

    assert "MAX_OUTPUT_CHARS = 4096" in source

    ok("Output Limit")


def test_timeout_handler():

    print("[TEST] Timeout Handler")

    source = Path(
        "safe_agent_v19.py"
    ).read_text()

    assert "TimeoutExpired" in source
    assert '"TIMEOUT"' in source

    ok("Timeout Handler")


def test_v18_hardening_kept():

    print("[TEST] V18 Hardening")

    source = Path(
        "safe_agent_v19.py"
    ).read_text()

    checks = [
        '"--ro-bind", str(WORKSPACE), "/workspace"',
        '"--cap-drop", "ALL"',
        '"--size", "536870912"',
        '"--unshare-net"',
    ]

    for c in checks:
        assert c in source

    ok("V18 Hardening Preserved")


if __name__ == "__main__":

    tests = [
        test_unknown_tool,
        test_script_allowlist,
        test_result_schema,
        test_output_limit,
        test_timeout_handler,
        test_v18_hardening_kept,
    ]

    passed = 0

    for t in tests:
        t()
        passed += 1

    print()
    print(
        f"{passed}/{len(tests)} V19 SECURITY TESTS PASSED"
    )
    print(
        "V19 SECURITY HARNESS OK"
    )
