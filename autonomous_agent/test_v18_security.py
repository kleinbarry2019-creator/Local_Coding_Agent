from pathlib import Path
import subprocess
import safe_agent_v18 as agent


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
        "FAIL: Angriff wurde nicht blockiert"
    )


def test_unknown_tool():
    blocked(
        "Unknown Tool",
        lambda: agent.execute_tool(
            "shell",
            {"cmd": "id"},
        ),
    )


def test_script_allowlist():
    blocked(
        "Script Allowlist",
        lambda: agent.execute_tool(
            "run_python_sandbox",
            {
                "script": "sandbox_v18_test.py"
            },
        ),
    )


def test_readonly_workspace():

    print("[TEST] Readonly Workspace")

    result = agent.run_python_sandbox(
        "safe_test.py"
    )

    assert result["returncode"] == 0
    assert "SAFE TEST OK" in result["stdout"]

    ok("Readonly Workspace")


def test_bwrap():

    print("[TEST] bubblewrap")

    r = subprocess.run(
        ["bwrap", "--version"],
        capture_output=True,
        text=True,
    )

    assert r.returncode == 0

    ok(r.stdout.strip())


def test_no_host_write():

    print("[TEST] Host Isolation")

    for f in [
        "/tmp/ESCAPE_TEST",
        "/var/ESCAPE_TEST",
        "/etc/ESCAPE_TEST",
    ]:
        assert not Path(f).exists()

    ok("Host Isolation")


def test_v18_features():

    print("[TEST] V18 Features")

    source = Path(
        "safe_agent_v18.py"
    ).read_text()

    checks = [
        '"--ro-bind", str(WORKSPACE), "/workspace"',
        '"--cap-drop", "ALL"',
        '"--size", "536870912"',
        '"--unshare-cgroup"',
        '"--unshare-net"',
    ]

    for c in checks:
        assert c in source, c

    ok("V18 Hardening")


if __name__ == "__main__":

    tests = [
        test_unknown_tool,
        test_script_allowlist,
        test_readonly_workspace,
        test_bwrap,
        test_no_host_write,
        test_v18_features,
    ]

    passed = 0

    for t in tests:
        t()
        passed += 1

    print()
    print(
        f"{passed}/{len(tests)} V18 SECURITY TESTS PASSED"
    )
    print(
        "V18 SECURITY HARNESS OK"
    )
