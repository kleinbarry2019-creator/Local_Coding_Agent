import safe_agent_v12 as agent


def must_fail(label, text):
    print("[TEST]", label)

    try:
        agent.parse_response(text)
    except RuntimeError as exc:
        print("       BLOCKED:", exc)
        return

    raise AssertionError(
        "Angriff/Fehlformat wurde NICHT blockiert"
    )


def test_multiple_json_objects():
    must_fail(
        "Mehrere Tool-Calls",
        (
            '{"name":"getcwd","arguments":{}}\n'
            '{"name":"list_files","arguments":{}}'
        ),
    )


def test_python_code():
    must_fail(
        "Python statt Tool-Call",
        (
            "with open('evil.txt', 'w') as f:\n"
            "    f.write('MUST NOT RUN')"
        ),
    )


def test_markdown():
    must_fail(
        "Markdown um JSON",
        '```json\n{"name":"getcwd","arguments":{}}\n```',
    )


def test_extra_field():
    must_fail(
        "Zusätzliches Feld",
        (
            '{"name":"getcwd","arguments":{},'
            '"evil":"payload"}'
        ),
    )


def test_valid_tool():
    result = agent.parse_response(
        '{"name":"getcwd","arguments":{}}'
    )

    assert result["type"] == "tool"
    assert result["name"] == "getcwd"

    print("[TEST] Gültiger Tool-Call")
    print("       PASS")


def test_valid_final():
    result = agent.parse_response(
        '{"final":"Alles erledigt."}'
    )

    assert result["type"] == "final"
    assert result["content"] == "Alles erledigt."

    print("[TEST] Gültige Final-Antwort")
    print("       PASS")


if __name__ == "__main__":
    test_multiple_json_objects()
    test_python_code()
    test_markdown()
    test_extra_field()
    test_valid_tool()
    test_valid_final()

    print()
    print("V12 PARSER TESTS OK")
