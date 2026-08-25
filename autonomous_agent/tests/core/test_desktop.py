from __future__ import annotations

from pathlib import Path

import pytest

from autonomous_agent.desktop import install_desktop_entry


def test_desktop_entry_is_installed_atomically(tmp_path: Path) -> None:
    target = tmp_path / "applications" / "acb.desktop"

    installed = install_desktop_entry(target)

    assert installed == target
    content = target.read_text(encoding="utf-8")
    assert "TryExec=\"" in content
    assert "Exec=\"" in content
    assert " app --project \"" in content
    assert " --state-dir \"" in content
    assert "ACB-Projects" in content
    assert target.stat().st_mode & 0o777 == 0o644
    assert not list(target.parent.glob(".acb.desktop.*"))


def test_desktop_entry_rejects_non_launcher_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        install_desktop_entry(tmp_path / "wrong.desktop")
