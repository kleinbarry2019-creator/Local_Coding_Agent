from importlib.metadata import entry_points, requires

import autonomous_agent


def test_package_version_and_console_script() -> None:
    assert autonomous_agent.__version__ == "50.8.0.dev1"
    scripts = {entry.name: entry.value for entry in entry_points(group="console_scripts")}
    assert scripts["acb"] == "autonomous_agent.cli:main"
    assert scripts["agent"] == "autonomous_agent.cli:main"
    assert requires("local-coding-agent") in (None, [])
