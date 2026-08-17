import json


ALLOWED_TOOLS = {"getcwd", "list_files", "write_file"}


def parse_tool_calls(content):
    calls = []

    for line in content.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(obj, dict):
            continue

        if "name" not in obj:
            continue

        calls.append(obj)

    return calls


def test_normal_python_is_rejected():
    content = """with open('agent_v9_test.txt', 'w') as file:
    file.write('MUST NOT RUN')"""

    calls = parse_tool_calls(content)

    assert calls == []
    print("PASS: normaler Python-Code wird nicht als Tool interpretiert.")


def test_valid_tool_is_accepted():
    content = '{"name":"write_file","arguments":{"path":"x.txt","content":"OK"}}'

    calls = parse_tool_calls(content)

    assert len(calls) == 1
    assert calls[0]["name"] == "write_file"
    print("PASS: gültiger Tool-Call wird erkannt.")


def test_unknown_tool_is_detected():
    content = '{"name":"exec","arguments":{"command":"rm -rf /"}}'

    calls = parse_tool_calls(content)

    assert len(calls) == 1
    assert calls[0]["name"] == "exec"
    assert calls[0]["name"] not in ALLOWED_TOOLS
    print("PASS: unbekanntes Tool wird erkannt und kann blockiert werden.")


def test_multiple_calls_are_detected():
    content = """{"name":"getcwd","arguments":{}}
{"name":"list_files","arguments":{}}"""

    calls = parse_tool_calls(content)

    assert len(calls) == 2
    print("PASS: mehrere Tool-Calls werden erkannt.")


if __name__ == "__main__":
    test_normal_python_is_rejected()
    test_valid_tool_is_accepted()
    test_unknown_tool_is_detected()
    test_multiple_calls_are_detected()

    print()
    print("V9 PARSER TESTS OK")
