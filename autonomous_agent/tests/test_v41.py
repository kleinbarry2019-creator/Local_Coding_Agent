from pathlib import Path
import safe_agent_v41 as agent


ROOT = agent.ROOT
RUNTIME = agent.RUNTIME


def test_schema():
    agent.validate_state({
        "version": 41,
        "tasks_completed": 0,
        "last_result": None,
    })

    bad = [
        {
            "version": 41,
            "tasks_completed": -1,
            "last_result": None,
        },
        {
            "version": 40,
            "tasks_completed": 0,
            "last_result": None,
        },
        {
            "version": 41,
            "tasks_completed": 0,
        },
    ]

    for state in bad:
        try:
            agent.validate_state(state)
        except RuntimeError:
            continue

        raise AssertionError(
            f"State wurde akzeptiert: {state!r}"
        )


def test_workspace_write():
    target = RUNTIME / "v41_test.txt"

    if target.exists():
        target.unlink()

    result = agent.sandbox_command(
        "printf 'V41_OK' > /workspace/v41_test.txt"
    )

    assert result["exit_code"] == 0
    assert target.read_text(
        encoding="utf-8"
    ) == "V41_OK"

    target.unlink()


def test_agent_code_readonly():
    result = agent.sandbox_command(
        "printf 'HACK' >> /agent/safe_agent_v41.py"
    )

    assert result["exit_code"] != 0


def test_agent_code_not_replaceable():
    result = agent.sandbox_command(
        "rm -f /agent/safe_agent_v41.py"
    )

    assert result["exit_code"] != 0
    assert (
        ROOT / "safe_agent_v41.py"
    ).exists()


def test_host_hidden():
    result = agent.sandbox_command(
        "python3 -c '"
        "from pathlib import Path; "
        "print(Path(\"/var/home/mklein\").exists()); "
        "print(Path(\"/etc/shadow\").exists()); "
        "print(Path(\"/sys\").exists())'"
    )

    assert result["exit_code"] == 0

    assert result["stdout"].splitlines() == [
        "False",
        "False",
        "False",
    ]


def test_agent_readonly_visible():
    result = agent.sandbox_command(
        "test -r /agent/safe_agent_v41.py"
    )

    assert result["exit_code"] == 0


def test_hash():
    state = {
        "version": 41,
        "tasks_completed": 1,
        "last_result": "OK",
    }

    digest = agent.state_digest(state)

    assert len(digest) == 64


if __name__ == "__main__":
    tests = [
        test_schema,
        test_workspace_write,
        test_agent_code_readonly,
        test_agent_code_not_replaceable,
        test_host_hidden,
        test_agent_readonly_visible,
        test_hash,
    ]

    for test in tests:
        print("[TEST]", test.__name__)
        test()

    print()
    print("V41 TESTS OK")
