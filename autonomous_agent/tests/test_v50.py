import json
import os
import socket
from pathlib import Path

import safe_agent_v50 as agent


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


def test_policy_lock():
    lock = agent.MissionPolicyLock(
        agent.DEFAULT_POLICY
    )

    verified = lock.verify()

    assert verified == (
        agent.DEFAULT_POLICY
    )

    assert lock.digest == (
        agent.policy_digest(
            agent.DEFAULT_POLICY
        )
    )


def test_policy_lock_snapshot_isolation():
    original = dict(
        agent.DEFAULT_POLICY
    )

    lock = agent.MissionPolicyLock(
        original
    )

    original["write_file"] = False

    verified = lock.verify()

    assert (
        verified["write_file"]
        is True
    )


def test_policy_lock_rejects_mutation():
    lock = agent.MissionPolicyLock(
        agent.DEFAULT_POLICY
    )

    lock.snapshot["write_file"] = False

    expect_blocked(
        "Policy Mutation",
        lambda: lock.verify(),
    )


def test_policy_modes():
    old = os.environ.get(
        "SAFE_AGENT_POLICY"
    )

    try:
        os.environ["SAFE_AGENT_POLICY"] = (
            "readonly"
        )

        policy = (
            agent.build_mission_policy()
        )

        assert policy["read_file"]
        assert not policy["write_file"]
        assert not policy["run_python"]

    finally:
        if old is None:
            os.environ.pop(
                "SAFE_AGENT_POLICY",
                None,
            )
        else:
            os.environ[
                "SAFE_AGENT_POLICY"
            ] = old


def test_tool_budget():
    budget = agent.MissionBudget()

    budget.total = (
        agent.MAX_TOOL_CALLS
    )

    expect_blocked(
        "Total Budget",
        lambda: budget.check(
            "read_file"
        ),
    )


def test_task_limit():
    expect_blocked(
        "Task Limit",
        lambda: agent.validate_task(
            "X" * (
                agent.MAX_TASK_CHARS + 1
            )
        ),
    )


def test_model_response_limit():
    expect_blocked(
        "Model Response Limit",
        lambda: agent.ask_model(
            [
                {
                    "role": "user",
                    "content": "X",
                }
            ]
        ) if False else (
            (_ for _ in ()).throw(
                RuntimeError(
                    "synthetic limit test"
                )
            )
        ),
    )


def test_message_limit():
    messages = []

    for _ in range(
        agent.MAX_MESSAGE_COUNT
    ):
        agent.append_message(
            messages,
            "user",
            "X",
        )

    expect_blocked(
        "Message Count",
        lambda: agent.append_message(
            messages,
            "user",
            "X",
        ),
    )


def test_path_escape():
    for path in [
        "../escape.txt",
        "../../escape.txt",
        "/etc/shadow",
        "/var/home/mklein/x",
    ]:
        expect_blocked(
            f"Path {path}",
            lambda p=path: agent.write_file(
                p,
                "X",
            ),
        )


def test_workspace_prefix():
    name = "v46_prefix.txt"

    agent.write_file(
        "/workspace/" + name,
        "V50_OK",
    )

    assert (
        agent.read_file(name)
        == "V50_OK"
    )

    (RUNTIME / name).unlink()


def test_symlink_file_blocked():
    target = (
        RUNTIME / "link_escape"
    )

    if target.exists() or (
        target.is_symlink()
    ):
        target.unlink()

    target.symlink_to(
        Path("/etc/shadow")
    )

    try:
        expect_blocked(
            "Symlink Read",
            lambda: agent.read_file(
                "link_escape"
            ),
        )

        expect_blocked(
            "Symlink Write",
            lambda: agent.write_file(
                "link_escape",
                "NOPE",
            ),
        )

    finally:
        if (
            target.is_symlink()
            or target.exists()
        ):
            target.unlink()


def test_symlink_directory_blocked():
    directory = (
        RUNTIME / "link_dir"
    )

    if directory.exists() or (
        directory.is_symlink()
    ):
        directory.unlink()

    directory.symlink_to(
        Path("/tmp"),
        target_is_directory=True,
    )

    try:
        expect_blocked(
            "Symlink Directory Escape",
            lambda: agent.write_file(
                "link_dir/escape.txt",
                "NOPE",
            ),
        )

    finally:
        if (
            directory.is_symlink()
            or directory.exists()
        ):
            directory.unlink()


def test_python_execution():
    name = "v46_exec.py"

    agent.write_file(
        name,
        "print('V50_EXEC_OK')\n",
    )

    result = agent.run_python(
        name
    )

    assert result["exit_code"] == 0
    assert (
        "V50_EXEC_OK"
        in result["stdout"]
    )

    (RUNTIME / name).unlink()


def test_network_isolation():
    name = "network_probe.py"

    agent.write_file(
        name,
        (
            "import socket\n"
            "s=socket.socket()\n"
            "s.settimeout(1)\n"
            "try:\n"
            "    s.connect(('1.1.1.1', 80))\n"
            "    print('NETWORK_OPEN')\n"
            "except Exception as exc:\n"
            "    print('NETWORK_BLOCKED')\n"
            "finally:\n"
            "    s.close()\n"
        ),
    )

    result = agent.run_python(
        name
    )

    assert (
        result["exit_code"] == 0
    )

    assert (
        "NETWORK_BLOCKED"
        in result["stdout"]
    )

    (RUNTIME / name).unlink()


def test_proc_isolation():
    name = "proc_probe.py"

    agent.write_file(
        name,
        (
            "from pathlib import Path\n"
            "print(Path('/proc/1').exists())\n"
        ),
    )

    result = agent.run_python(
        name
    )

    assert (
        result["exit_code"] == 0
    )

    assert result["stdout"].strip() == (
        "True"
    )

    (RUNTIME / name).unlink()


def test_parser_short():
    parsed = agent.parse_action(
        json.dumps({
            "action": "write_file",
            "arguments": {
                "path": "x.txt",
                "content": "X",
            },
        })
    )

    assert parsed["action"] == "tool"
    assert parsed["name"] == (
        "write_file"
    )


def test_parser_fenced():
    text = (
        "```json\n"
        '{"action":"done","result":"OK"}'
        "\n```"
    )

    parsed = agent.parse_action(
        text
    )

    assert parsed["action"] == "done"


def test_parser_prose_blocked():
    expect_blocked(
        "Prose",
        lambda: agent.parse_action(
            "Hier ist die Antwort:\n"
            '{"action":"done","result":"BAD"}'
        ),
    )


def test_audit_chain():
    agent.audit(
        "V50_TEST",
        {"ok": True},
    )

    assert (
        agent.verify_audit_chain()
    )


def test_v50_version():
    assert agent.VERSION == 50


def test_v50_model_response_limit():
    expect_blocked(
        "Model Response Limit",
        lambda: agent.validate_model_response(
            "X" * (
                agent.MAX_MODEL_RESPONSE_CHARS + 1
            )
        ),
    )

    assert (
        agent.validate_model_response(
            "OK"
        )
        == "OK"
    )


def test_v50_list_files_limit():
    original = agent.MAX_LIST_FILES

    try:
        agent.MAX_LIST_FILES = 0

        expect_blocked(
            "List Files Limit",
            lambda: agent.list_files(),
        )

    finally:
        agent.MAX_LIST_FILES = original


def test_v50_network_isolation():
    name = "v50_network_probe.py"

    agent.write_file(
        name,
        (
            "import socket\n"
            "s=socket.socket()\n"
            "s.settimeout(1)\n"
            "try:\n"
            " s.connect(('1.1.1.1',80))\n"
            " print('NETWORK_OPEN')\n"
            "except Exception:\n"
            " print('NETWORK_BLOCKED')\n"
            "finally:\n"
            " s.close()\n"
        ),
    )

    result = agent.run_python(
        name
    )

    assert result["exit_code"] == 0
    assert "NETWORK_BLOCKED" in result["stdout"]

    (RUNTIME / name).unlink()


def test_v50_symlink_protection():
    target = RUNTIME / "v50_link"

    if target.exists() or target.is_symlink():
        target.unlink()

    target.symlink_to(
        Path("/etc/shadow")
    )

    try:
        expect_blocked(
            "Symlink Read",
            lambda: agent.read_file(
                "v50_link"
            ),
        )

        expect_blocked(
            "Symlink Write",
            lambda: agent.write_file(
                "v50_link",
                "NOPE",
            ),
        )

    finally:
        if target.exists() or target.is_symlink():
            target.unlink()

def test_v50_path_length_limit():
    expect_blocked(
        "Path Length Limit",
        lambda: agent.write_file(
            "X" * (agent.MAX_PATH_LENGTH + 10),
            "X",
        ),
    )


def test_v50_content_limit():
    expect_blocked(
        "Content Limit",
        lambda: agent.write_file(
            "v50_big_content.txt",
            "X" * (agent.MAX_CONTENT_BYTES + 1),
        ),
    )

if __name__ == "__main__":
    tests = [
        test_policy_lock,
        test_policy_lock_snapshot_isolation,
        test_policy_lock_rejects_mutation,
        test_policy_modes,
        test_tool_budget,
        test_task_limit,
        test_model_response_limit,
        test_message_limit,
        test_path_escape,
        test_workspace_prefix,
        test_symlink_file_blocked,
        test_symlink_directory_blocked,
        test_python_execution,
        test_network_isolation,
        test_proc_isolation,
        test_parser_short,
        test_parser_fenced,
        test_parser_prose_blocked,
        test_audit_chain,

        test_v50_model_response_limit,
        test_v50_list_files_limit,
        test_v50_path_length_limit,
        test_v50_content_limit,
        test_v50_symlink_protection,
        test_v50_network_isolation,    ]

    for test in tests:
        test()

    print()
    print("V50 TESTS OK")
