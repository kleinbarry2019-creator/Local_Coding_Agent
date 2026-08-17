from pathlib import Path
import json
import safe_agent_v9 as agent


WORKSPACE = Path.cwd().resolve()


def expect_block(description, func):
    print(f"[TEST] {description}")
    try:
        func()
    except PermissionError as exc:
        print(f"       BLOCKED: {exc}")
        return
    raise AssertionError("Angriff wurde NICHT blockiert")


def test_unknown_tool():
    expect_block(
        "Unbekanntes Tool",
        lambda: agent.execute_tool(
            "exec",
            {"command": "rm -rf /"}
        ),
    )


def test_path_traversal():
    expect_block(
        "Path Traversal ../",
        lambda: agent.execute_tool(
            "write_file",
            {
                "path": "../escape_test.txt",
                "content": "MUST NOT WRITE",
            },
        ),
    )


def test_absolute_path():
    outside = WORKSPACE.parent / "absolute_escape_test.txt"

    expect_block(
        "Absoluter Pfad außerhalb Sandbox",
        lambda: agent.execute_tool(
            "write_file",
            {
                "path": str(outside),
                "content": "MUST NOT WRITE",
            },
        ),
    )


def test_workspace_overwrite():
    expect_block(
        "Workspace selbst überschreiben",
        lambda: agent.execute_tool(
            "write_file",
            {
                "path": ".",
                "content": "MUST NOT WRITE",
            },
        ),
    )


def test_missing_arguments():
    expect_block(
        "Fehlendes write_file.path",
        lambda: agent.execute_tool(
            "write_file",
            {
                "content": "MUST NOT WRITE",
            },
        ),
    )


def test_wrong_argument_type():
    expect_block(
        "Falscher Argumenttyp",
        lambda: agent.execute_tool(
            "write_file",
            {
                "path": 123,
                "content": "MUST NOT WRITE",
            },
        ),
    )


def test_extra_arguments_getcwd():
    expect_block(
        "Unerlaubte Zusatzargumente für getcwd",
        lambda: agent.execute_tool(
            "getcwd",
            {
                "evil": "payload",
            },
        ),
    )


def test_extra_arguments_list_files():
    expect_block(
        "Unerlaubte Zusatzargumente für list_files",
        lambda: agent.execute_tool(
            "list_files",
            {
                "path": "/etc",
            },
        ),
    )


def test_normal_write_still_works():
    print("[TEST] Normaler Schreibzugriff")

    target = WORKSPACE / "v10_safe_test.txt"

    if target.exists():
        target.unlink()

    result = agent.execute_tool(
        "write_file",
        {
            "path": "v10_safe_test.txt",
            "content": "V10 SAFE WRITE OK",
        },
    )

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "V10 SAFE WRITE OK"
    assert result["bytes"] == len("V10 SAFE WRITE OK".encode())

    target.unlink()

    print("       PASS")


if __name__ == "__main__":
    tests = [
        test_unknown_tool,
        test_path_traversal,
        test_absolute_path,
        test_workspace_overwrite,
        test_missing_arguments,
        test_wrong_argument_type,
        test_extra_arguments_getcwd,
        test_extra_arguments_list_files,
        test_normal_write_still_works,
    ]

    passed = 0

    for test in tests:
        test()
        passed += 1

    print()
    print(f"{passed}/{len(tests)} SECURITY TESTS PASSED")
    print("V10 SECURITY HARNESS OK")
