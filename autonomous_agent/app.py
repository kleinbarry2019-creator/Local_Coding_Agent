"""Offline GTK desktop application for ACB.

GTK is intentionally an optional host integration. The installable Python
package stays dependency-free; on Linux systems with PyGObject available the
CLI hands the app to the system Python so the desktop binding remains local
and offline.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404 - fixed local GTK interpreter handoff only
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autonomous_agent.core.config import (
    AgentConfig,
    CliOverrides,
    ConfigError,
    ExecutionMode,
    load_config,
)
from autonomous_agent.ui import RuntimeTaskController, UiTask

APP_NAME = "ACB – Autonome Computing Butler"
_SYSTEM_PYTHON = "/usr/bin/python3"


def launch(config: AgentConfig) -> int:
    """Launch the desktop app, re-executing in the host GTK Python if needed."""
    if _gtk_available():
        return _run_gtk(config)
    if Path(_SYSTEM_PYTHON).is_file() and Path(sys.executable).resolve() != Path(
        _SYSTEM_PYTHON
    ).resolve():
        package_parent = str(Path(__file__).resolve().parent.parent)
        environment = dict(os.environ)
        existing_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            package_parent
            if not existing_path
            else f"{package_parent}{os.pathsep}{existing_path}"
        )
        command = [
            _SYSTEM_PYTHON,
            "-m",
            "autonomous_agent.app",
            "--project",
            str(config.paths.project_root),
            "--state-dir",
            str(config.paths.state_root),
        ]
        completed = subprocess.run(  # nosec B603 - fixed argv, no shell, local only
            command, env=environment, check=False
        )
        return completed.returncode
    sys.stderr.write(
        "acb: desktop app requires GTK 4 and PyGObject on the local system.\n"
    )
    return 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acb app")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    try:
        namespace = parser.parse_args(None if argv is None else list(argv))
        config = load_config(
            cwd=Path.cwd(),
            home=Path.home().resolve(strict=False),
            environ=dict(os.environ),
            cli=CliOverrides(
                project_root=namespace.project,
                state_root=namespace.state_dir,
                mode=ExecutionMode.AUTONOMOUS,
            ),
        )
        return _run_gtk(config)
    except (ConfigError, ValueError):
        sys.stderr.write("acb: desktop app configuration is invalid.\n")
        return 2


def _gtk_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk

        del GLib, Gtk
    except (ImportError, ValueError):
        return False
    return True


def _run_gtk(config: AgentConfig) -> int:
    try:
        import gi

        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError):
        sys.stderr.write(
            "acb: desktop app requires GTK 4 and PyGObject on the local system.\n"
        )
        return 3

    class DesktopApplication(Gtk.Application):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(application_id="com.acb.AutonomeComputingButler")
            research_enabled = os.environ.get("ACB_RESEARCH_NETWORK", "1").lower() not in {
                "0",
                "false",
                "off",
                "no",
            }
            self.controller = RuntimeTaskController(
                config,
                start_learning=True,
                research_network=research_enabled,
            )
            self.window: Any = None
            self.current: Any = None
            self.history: Any = None
            self.input: Any = None
            self.send: Any = None
            self.stack: Any = None
            self.knowledge_view: Any = None
            self.suggestion_view: Any = None
            self.update_view: Any = None
            self.account_status: Any = None
            self.recovered = self.controller.recover_pending()

        def do_activate(self) -> None:
            if self.window is None:
                self._build_window()
            self.window.present()

        def do_shutdown(self) -> None:
            self.controller.close()
            super().do_shutdown()

        def _build_window(self) -> None:
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title(APP_NAME)
            self.window.set_default_size(1080, 760)
            self.window.connect("close-request", self._close_request)

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            outer.add_css_class("app-shell")
            self.window.set_child(outer)
            outer.append(self._header())

            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            content.set_vexpand(True)
            outer.append(content)
            self.stack = Gtk.Stack()
            self.stack.set_hexpand(True)
            self.stack.set_vexpand(True)
            content.append(self._sidebar())
            content.append(self.stack)

            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            main.set_hexpand(True)
            main.set_margin_top(24)
            main.set_margin_bottom(24)
            main.set_margin_start(28)
            main.set_margin_end(28)

            title = Gtk.Label(label="Neuer Auftrag")
            title.set_xalign(0)
            title.add_css_class("section-title")
            main.append(title)

            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            scroll.set_hexpand(True)
            self.current = Gtk.Label(
                label="ACB ist bereit. Aufgaben werden lokal ausgeführt und persistiert."
            )
            self.current.set_wrap(True)
            self.current.set_xalign(0)
            self.current.set_yalign(0)
            self.current.set_valign(Gtk.Align.START)
            self.current.add_css_class("conversation")
            scroll.set_child(self.current)
            main.append(scroll)

            composer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            composer.add_css_class("composer")
            self.input = Gtk.Entry()
            self.input.set_hexpand(True)
            self.input.set_placeholder_text(
                "Zum Beispiel: Erstelle notes.txt mit dem Inhalt Hallo ACB"
            )
            self.input.connect("activate", self._submit)
            composer.append(self.input)
            self.send = Gtk.Button(label="Ausführen")
            self.send.add_css_class("suggested-action")
            self.send.connect("clicked", self._submit)
            composer.append(self.send)
            main.append(composer)

            self.stack.add_titled(main, "tasks", "Aufträge")
            self.stack.add_titled(self._knowledge_page(), "knowledge", "Wissen & Recherche")
            self.stack.add_titled(
                self._suggestion_page(), "improvements", "Verbesserungen"
            )
            self.stack.add_titled(
                self._self_update_page(), "self-development", "Selbstentwicklung"
            )
            self.stack.add_titled(self._account_page(), "account", "Konto & Sync")

            self._install_css(Gdk, Gtk)
            for task in self.recovered:
                self._show_task(task, recovered=True)
            GLib.timeout_add(400, self._refresh)

        def _knowledge_page(self) -> Any:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)
            title = Gtk.Label(label="Wissen & Recherche")
            title.set_xalign(0)
            title.add_css_class("section-title")
            page.append(title)
            status = Gtk.Label(label="Lokale Wissensbasis wird geladen …")
            status.set_xalign(0)
            status.set_wrap(True)
            status.add_css_class("muted")
            page.append(status)
            research = Gtk.Button(label="Jetzt nach vertrauenswürdigen Quellen suchen")
            research.set_halign(Gtk.Align.START)
            research.connect("clicked", self._research_now)
            page.append(research)
            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            self.knowledge_view = Gtk.Label(label="Noch keine Quellen gespeichert.")
            self.knowledge_view.set_xalign(0)
            self.knowledge_view.set_yalign(0)
            self.knowledge_view.set_wrap(True)
            self.knowledge_view.add_css_class("conversation")
            scroll.set_child(self.knowledge_view)
            page.append(scroll)
            return page

        def _suggestion_page(self) -> Any:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)
            title = Gtk.Label(label="Verbesserungsvorschläge")
            title.set_xalign(0)
            title.add_css_class("section-title")
            page.append(title)
            note = Gtk.Label(
                label="Vorschläge werden aus Aufgaben und geprüften Quellen abgeleitet. "
                "Übernahmen bleiben durch Tests, Security-Scan und Rollback geschützt."
            )
            note.set_xalign(0)
            note.set_wrap(True)
            note.add_css_class("muted")
            page.append(note)
            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            self.suggestion_view = Gtk.Label(label="Noch keine Vorschläge.")
            self.suggestion_view.set_xalign(0)
            self.suggestion_view.set_yalign(0)
            self.suggestion_view.set_wrap(True)
            self.suggestion_view.add_css_class("conversation")
            scroll.set_child(self.suggestion_view)
            page.append(scroll)
            return page

        def _self_update_page(self) -> Any:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)
            title = Gtk.Label(label="Selbstentwicklung")
            title.set_xalign(0)
            title.add_css_class("section-title")
            page.append(title)
            note = Gtk.Label(
                label="ACB beobachtet neue KI-, Netzwerk- und PC-Sicherheitsinformationen. "
                "Eine Änderung wird erst nach Ressourcenprüfung, Tests, Security-Gate "
                "und rückrollbarem Checkpoint aktiv."
            )
            note.set_xalign(0)
            note.set_wrap(True)
            note.add_css_class("muted")
            page.append(note)
            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            self.update_view = Gtk.Label(label="Noch keine Selbstentwicklungs-Vorschläge.")
            self.update_view.set_xalign(0)
            self.update_view.set_yalign(0)
            self.update_view.set_wrap(True)
            self.update_view.add_css_class("conversation")
            scroll.set_child(self.update_view)
            page.append(scroll)
            return page

        def _account_page(self) -> Any:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)
            title = Gtk.Label(label="Lokales Konto & spätere Synchronisierung")
            title.set_xalign(0)
            title.add_css_class("section-title")
            page.append(title)
            note = Gtk.Label(
                label="Konten werden lokal als sichere Passwort-Hashes gespeichert. "
                "Die Sync-Struktur ist vorbereitet, aber es wird noch kein Netzwerk-Konto "
                "und keine Cloud-Verbindung angelegt."
            )
            note.set_xalign(0)
            note.set_wrap(True)
            note.add_css_class("muted")
            page.append(note)
            username = Gtk.Entry()
            username.set_placeholder_text("Benutzername (3–32 Zeichen)")
            page.append(username)
            password = Gtk.Entry()
            password.set_placeholder_text("Passwort (mindestens 10 Zeichen)")
            password.set_visibility(False)
            page.append(password)
            create = Gtk.Button(label="Lokales Konto anlegen")
            create.set_halign(Gtk.Align.START)
            create.connect("clicked", lambda *_args: self._create_account(username, password))
            page.append(create)
            self.account_status = Gtk.Label(label="Noch kein lokales Konto angelegt.")
            self.account_status.set_xalign(0)
            self.account_status.set_wrap(True)
            self.account_status.add_css_class("muted")
            page.append(self.account_status)
            return page

        def _header(self) -> Any:
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            bar.add_css_class("topbar")
            brand = Gtk.Label(label="ACB")
            brand.add_css_class("brand")
            bar.append(brand)
            subtitle = Gtk.Label(label="Autonome Computing Butler")
            subtitle.add_css_class("subtitle")
            bar.append(subtitle)
            offline = Gtk.Label(label="● OFFLINE · LOKAL")
            offline.set_hexpand(True)
            offline.set_xalign(1)
            offline.add_css_class("offline")
            bar.append(offline)
            return bar

        def _sidebar(self) -> Any:
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            sidebar.set_size_request(250, -1)
            sidebar.add_css_class("sidebar")
            if self.stack is not None:
                switcher = Gtk.StackSwitcher()
                switcher.set_stack(self.stack)
                switcher.set_halign(Gtk.Align.FILL)
                sidebar.append(switcher)
            project_title = Gtk.Label(label="Arbeitsbereich")
            project_title.set_xalign(0)
            project_title.add_css_class("section-title")
            sidebar.append(project_title)
            project = Gtk.Label(label=str(config.paths.project_root))
            project.set_wrap(True)
            project.set_xalign(0)
            project.add_css_class("muted")
            sidebar.append(project)
            state = Gtk.Label(label="Persistenter Task-State aktiv")
            state.set_wrap(True)
            state.set_xalign(0)
            state.add_css_class("state-good")
            sidebar.append(state)
            history_title = Gtk.Label(label="Aufgabenverlauf")
            history_title.set_xalign(0)
            history_title.set_margin_top(22)
            history_title.add_css_class("section-title")
            sidebar.append(history_title)
            self.history = Gtk.ListBox()
            self.history.set_selection_mode(Gtk.SelectionMode.NONE)
            sidebar.append(self.history)
            return sidebar

        def _submit(self, *_args: object) -> None:
            goal = self.input.get_text().strip()
            if not goal:
                return
            task = self.controller.submit(goal)
            self.input.set_text("")
            self.send.set_sensitive(False)
            self._show_task(task, recovered=False)

        def _show_task(self, task: UiTask, *, recovered: bool) -> None:
            prefix = "Fortsetzung" if recovered else "Auftrag"
            self.current.set_text(f"{prefix}: {task.goal}\nStatus: {task.status}")
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{task.status} · {task.goal}")
            label.set_wrap(True)
            label.set_xalign(0)
            row.set_child(label)
            self.history.prepend(row)

        def _refresh(self) -> bool:
            tasks = self.controller.tasks()
            active = next(
                (item for item in tasks if item.status in {"queued", "running", "recovering"}),
                None,
            )
            if active is None:
                self.send.set_sensitive(True)
            else:
                self.current.set_text(f"{active.goal}\nStatus: {active.status}")
            self._refresh_learning()
            return True

        def _refresh_learning(self) -> None:
            status = self.controller.learning_status()
            if self.knowledge_view is not None:
                items = self.controller.knowledge(30)
                self.knowledge_view.set_text(
                    "\n\n".join(
                        f"{item.title}\n{item.source} · {item.topic}\n{item.url}\n{item.summary}"
                        for item in items
                    )
                    or "Noch keine Quellen gespeichert."
                )
            if self.suggestion_view is not None:
                suggestions = self.controller.suggestions(30)
                self.suggestion_view.set_text(
                    "\n\n".join(
                        f"[{item.priority}] {item.title}\n{item.description}\nStatus: {item.status}"
                        for item in suggestions
                    )
                    or "Noch keine Vorschläge."
                )
            if self.update_view is not None:
                updates = self.controller.self_updates(30)
                self.update_view.set_text(
                    "\n\n".join(
                        f"{item.title}\n{item.reason}\nStatus: {item.status} · Gate: {item.gate_status}"
                        for item in updates
                    )
                    or "Noch keine Selbstentwicklungs-Vorschläge."
                )
            if self.account_status is not None:
                accounts = self.controller.learning.store.accounts()
                self.account_status.set_text(
                    f"{len(accounts)} lokales Konto/Konten · Gerät: "
                    f"{self.controller.sync_manifest().get('device_id')}\n"
                    f"Letzte Recherche: {status.last_research_at or 'noch nicht'} · "
                    f"Netzwerk: {'aktiv' if status.network_enabled else 'offline'}"
                )

        def _research_now(self, *_args: object) -> None:
            threading.Thread(
                target=self.controller.research_now,
                name="acb-research-now",
                daemon=True,
            ).start()

        def _create_account(self, username: Any, password: Any) -> None:
            try:
                account = self.controller.create_account(
                    username.get_text().strip(), password.get_text()
                )
            except ValueError:
                self.account_status.set_text("Konto konnte nicht angelegt werden. Prüfe Benutzername und Passwort.")
                return
            password.set_text("")
            self.account_status.set_text(
                f"Konto {account.username} lokal angelegt. Geräte-ID: {account.device_id}"
            )

        def _close_request(self, *_args: object) -> bool:
            self.controller.close()
            return False

        @staticmethod
        def _install_css(gdk: Any, gtk: Any) -> None:
            css = gtk.CssProvider()
            css.load_from_data(
                b"""
                .app-shell { background: #0b1220; color: #e7edf8; }
                .topbar { background: #111c2f; padding: 20px 28px; }
                .brand { color: #5eead4; font-size: 27px; font-weight: 800; }
                .subtitle, .muted { color: #9fb0ca; }
                .offline { color: #5eead4; font-size: 12px; font-weight: 700; }
                .sidebar { background: #0e1829; padding: 24px 18px; }
                .section-title { color: #dbeafe; font-size: 16px; font-weight: 700; }
                .state-good { color: #5eead4; font-size: 12px; }
                .conversation { background: #121c2e; border-radius: 14px; padding: 24px; font-size: 17px; }
                .composer { background: #121c2e; border-radius: 12px; padding: 12px; }
                button.suggested-action { background: #5eead4; color: #052e2b; font-weight: 700; }
                """
            )
            gtk.StyleContext.add_provider_for_display(
                gdk.Display.get_default(), css, gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    application = DesktopApplication()
    return application.run([])


__all__ = ["APP_NAME", "launch", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
