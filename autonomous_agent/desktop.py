"""User-local desktop integration for the offline ACB application."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

DESKTOP_FILENAME = "acb.desktop"
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=ACB – Autonome Computing Butler
Comment=Lokaler Offline-Agent für Coding- und Systemaufgaben
Exec=acb app
Terminal=false
Categories=Development;Utility;
Keywords=ACB;Agent;Offline;Coding;
"""


def _desktop_quote(value: Path) -> str:
    """Quote one absolute path for the freedesktop Exec field."""
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_desktop_entry(executable: Path) -> str:
    home = Path.home().resolve(strict=False)
    project = home / "ACB-Projects"
    state = home / ".local" / "state" / "local-coding-agent"
    return f"""[Desktop Entry]
Type=Application
Name=ACB – Autonome Computing Butler
Comment=Lokaler Offline-Agent für Coding- und Systemaufgaben
TryExec={_desktop_quote(executable)}
Exec={_desktop_quote(executable)} app --project {_desktop_quote(project)} --state-dir {_desktop_quote(state)}
Terminal=false
Categories=Development;Utility;
Keywords=ACB;Agent;Offline;Coding;
"""


def install_desktop_entry(target: Path | None = None) -> Path:
    """Install a user-local launcher atomically and return its path."""
    destination = (
        Path.home() / ".local" / "share" / "applications" / DESKTOP_FILENAME
        if target is None
        else target
    )
    destination = destination.expanduser()
    if not destination.is_absolute() or destination.name != DESKTOP_FILENAME:
        raise ValueError("desktop target must be an absolute acb.desktop path")
    project = Path.home().resolve(strict=False) / "ACB-Projects"
    project.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = Path.home().resolve(strict=False) / ".local" / "state" / "local-coding-agent"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    executable = Path(shutil.which("acb") or "acb").resolve(strict=False)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{DESKTOP_FILENAME}.", dir=destination.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(_render_desktop_entry(executable))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return destination


__all__ = ["DESKTOP_ENTRY", "DESKTOP_FILENAME", "install_desktop_entry"]
