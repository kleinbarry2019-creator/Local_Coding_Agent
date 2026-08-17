from pathlib import Path
import safe_agent_v40 as agent


ROOT = agent.ROOT


def test_schema():
    agent.validate_state({
        "version": 40,
        "tasks_completed": 0,
        "last_result": None,
    })

    bad_states = [
        {
            "version": 40,
            "tasks_completed": -1,
            "last_result": None,
        },
        {
            "version": 40,
            "tasks_completed": 0,
        },
        {
            "version": 39,
            "tasks_completed": 0,
            "last_result": None,
        },
    ]

    for state in bad_states:
        try:
            agent.validate_state(state)
        except RuntimeError:
            continue
        raise AssertionError(
            f"Ungültiger State akzeptiert: {state!r}"
        )


def test_parser():
    result = agent.parse_action(
        '{"action":"done","result":"OK"}'
    )

    assert result["action"] == "done"

    try:
        agent.parse_action(
            '{"action":"run_command","command":"echo OK"}\n'
            '{"action":"done","result":"oops"}'
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "Mehrere JSON-Objekte wurden akzeptiert"
        )


def test_sandbox_visibility():
    result = agent.sandbox_command(
        "python3 -c '"
        "from pathlib import Path; "
        "print(Path(\"/var/home/mklein\").exists()); "
        "print(Path(\"/etc/shadow\").exists()); "
        "print(Path(\"/sys\").exists())'"
    )

    assert result["exit_code"] == 0

    lines = result["stdout"].splitlines()
    assert lines == ["False", "False", "False"]


def test_workspace_write():
    target = ROOT / "v40_test.txt"

    if target.exists():
        target.unlink()

    result = agent.sandbox_command(
        "printf 'V40_OK' > /workspace/v40_test.txt"
    )

    assert result["exit_code"] == 0
    assert target.read_text(
        encoding="utf-8"
    ) == "V40_OK"

    target.unlink()


def test_state_hash_roundtrip():
    state = {
        "version": 40,
        "tasks_completed": 3,
        "last_result": "OK",
    }

    digest = agent.state_digest(state)

    assert len(digest) == 64
    assert digest == agent.state_digest(state)


if __name__ == "__main__":
    tests = [
        test_schema,
        test_parser,
        test_sandbox_visibility,
        test_workspace_write,
        test_state_hash_roundtrip,
    ]

    for test in tests:
        print("[TEST]", test.__name__)
        test()

    print()
    print("V40 TESTS OK")
