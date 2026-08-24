from __future__ import annotations

import json
from pathlib import Path

from autonomous_agent import cli


def test_cli_run_executes_and_reports_verified_completion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    state = tmp_path / "state"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))

    exit_code = cli.main(
        [
            "run",
            "Write `cli-e2e.txt` with content CLI_OK",
            "--project",
            str(project),
            "--state-dir",
            str(state),
            "--json",
        ]
    )

    output = capsys.readouterr()
    document = json.loads(output.out)
    assert exit_code == 0
    assert output.err == ""
    assert document["status"] == "completed"
    assert document["completion"]["e2e_verified"] is True
    assert (project / "cli-e2e.txt").read_text() == "CLI_OK"


def test_cli_run_returns_nonzero_when_real_command_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    state = tmp_path / "state"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))

    exit_code = cli.main(
        [
            "run",
            "Run python3 -c 'raise SystemExit(9)'",
            "--project",
            str(project),
            "--state-dir",
            str(state),
            "--json",
        ]
    )

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert document["status"] == "failed"
    assert document["completion"]["completed"] is False
