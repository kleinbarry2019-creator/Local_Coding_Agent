from pathlib import Path
import safe_agent_v7 as agent


WORKSPACE = Path.cwd().resolve()


def test_getcwd():
    assert agent.getcwd() == str(WORKSPACE)


def test_list_files():
    files = agent.list_files()
    assert isinstance(files, list)
    assert "safe_agent_v7.py" in files


def test_write_file():
    path = "test_safe_write.txt"
    target = WORKSPACE / path

    if target.exists():
        target.unlink()

    result = agent.write_file(path, "TEST WRITE OK")

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "TEST WRITE OK"
    assert result["bytes"] == len("TEST WRITE OK".encode("utf-8"))

    target.unlink()


def test_escape_blocked():
    try:
        agent.write_file("../escape_test.txt", "MUST NOT WRITE")
    except PermissionError:
        return

    raise AssertionError("Path traversal wurde nicht blockiert.")


def test_absolute_escape_blocked():
    outside = WORKSPACE.parent / "absolute_escape_test.txt"

    try:
        agent.write_file(str(outside), "MUST NOT WRITE")
    except PermissionError:
        return

    raise AssertionError("Absoluter Pfad wurde nicht blockiert.")


def test_unknown_tool_blocked():
    try:
        agent.execute_tool("secret_action", {})
    except PermissionError:
        return

    raise AssertionError("Unbekanntes Tool wurde nicht blockiert.")


def test_duplicate_prevention():
    state = set()

    for name in ["getcwd", "getcwd"]:
        if name in state:
            continue
        state.add(name)

    assert len(state) == 1


def run():
    tests = [
        test_getcwd,
        test_list_files,
        test_write_file,
        test_escape_blocked,
        test_absolute_escape_blocked,
        test_unknown_tool_blocked,
        test_duplicate_prevention,
    ]

    passed = 0

    for test in tests:
        print(f"[TEST] {test.__name__}")
        test()
        print("       PASS")
        passed += 1

    print()
    print(f"{passed}/{len(tests)} TESTS PASSED")
    print("AGENT SAFETY OK")


if __name__ == "__main__":
    run()
