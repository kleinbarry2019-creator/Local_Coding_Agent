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
from autonomous_agent.core.preferences import THEMES
from autonomous_agent.ui import RuntimeTaskController, UiTask

APP_NAME = "ACB – Autonome Computing Butler"
_SYSTEM_PYTHON = "/usr/bin/python3"


def launch(config: AgentConfig) -> int:
    """Launch the desktop app, re-executing in the host GTK Python if needed."""
    if _gtk_available():
        return _run_gtk(config)
    # UV/venv interpreters may resolve to the same system binary while still
    # lacking PyGObject on their import path. Compare the executable spelling
    # so the host GTK interpreter is used when the packaged tool is launched.
    if Path(_SYSTEM_PYTHON).is_file() and Path(sys.executable) != Path(_SYSTEM_PYTHON):
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
            self.experience_view: Any = None
            self.account_status: Any = None
            self.preference_status: Any = None
            self.preference_controls: dict[str, Any] = {}
            self.onboarding_status_view: Any = None
            self.undo_button: Any = None
            self.retry_button: Any = None
            self.audit_button: Any = None
            self.log_window: Any = None
            self.speak_button: Any = None
            self.voice_thread: threading.Thread | None = None
            self.feedback_box: Any = None
            self.feedback_rating: Any = None
            self.feedback_comment: Any = None
            self.feedback_status: Any = None
            self.feedback_session: str | None = None
            self.research_thread: threading.Thread | None = None
            self.persisted_history = self.controller.persisted_sessions()
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
            self.current.set_selectable(True)
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
            main.append(self._feedback_panel())

            self.stack.add_titled(self._welcome_page(), "welcome", "Willkommen")
            self.stack.add_titled(main, "tasks", "Aufträge")
            self.stack.add_titled(self._knowledge_page(), "knowledge", "Wissen & Recherche")
            self.stack.add_titled(
                self._suggestion_page(), "improvements", "Verbesserungen"
            )
            self.stack.add_titled(
                self._self_update_page(), "self-development", "Selbstentwicklung"
            )
            self.stack.add_titled(self._account_page(), "account", "Konto & Sync")
            self.stack.add_titled(self._settings_page(), "settings", "Einstellungen")

            self._install_css(Gdk, Gtk)
            if self.controller.onboarding_status().phase == "ready":
                self.stack.set_visible_child_name("tasks")
            for session in reversed(self.persisted_history):
                if session.get("status") in {"completed", "failed"}:
                    self._show_persisted_session(session)
            for task in self.recovered:
                self._show_task(task, recovered=True)
            GLib.timeout_add(400, self._refresh)

        def _feedback_panel(self) -> Any:
            panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            panel.add_css_class("feedback-panel")
            panel.set_visible(False)
            title = Gtk.Label(label="Auftrag bewerten")
            title.set_xalign(0)
            title.add_css_class("section-title")
            panel.append(title)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.feedback_rating = Gtk.ComboBoxText()
            for rating in range(1, 11):
                self.feedback_rating.append(str(rating), str(rating))
            self.feedback_rating.set_active_id("10")
            row.append(self.feedback_rating)
            self.feedback_comment = Gtk.Entry()
            self.feedback_comment.set_hexpand(True)
            self.feedback_comment.set_placeholder_text("Optionaler Kommentar")
            row.append(self.feedback_comment)
            save = Gtk.Button(label="Bewertung speichern")
            save.connect("clicked", self._save_feedback)
            row.append(save)
            panel.append(row)
            self.feedback_status = Gtk.Label(label="")
            self.feedback_status.set_xalign(0)
            self.feedback_status.add_css_class("muted")
            panel.append(self.feedback_status)
            self.feedback_box = panel
            return panel

        def _welcome_page(self) -> Any:
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            page.set_margin_top(42)
            page.set_margin_bottom(42)
            page.set_margin_start(42)
            page.set_margin_end(42)
            title = Gtk.Label(label="Willkommen bei ACB – deinem persönlichen Computing Butler")
            title.set_xalign(0)
            title.set_wrap(True)
            title.add_css_class("section-title")
            page.append(title)
            intro = Gtk.Label(
                label=(
                    "ACB arbeitet lokal und offline weiter. Datenschutz steht an erster Stelle: "
                    "Für den Start sind keine E-Mail-, Telefon- oder Adressdaten nötig. "
                    "Du kannst zuerst drei Tage testen oder das Profil direkt einrichten."
                )
            )
            intro.set_xalign(0)
            intro.set_wrap(True)
            intro.add_css_class("conversation")
            page.append(intro)
            self.onboarding_status_view = Gtk.Label(label="")
            self.onboarding_status_view.set_xalign(0)
            self.onboarding_status_view.set_wrap(True)
            self.onboarding_status_view.add_css_class("muted")
            page.append(self.onboarding_status_view)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            trial = Gtk.Button(label="Erstmal testen")
            trial.add_css_class("suggested-action")
            trial.connect("clicked", self._start_trial)
            actions.append(trial)
            setup = Gtk.Button(label="Schieß los – Profil einrichten")
            setup.connect("clicked", self._start_setup)
            actions.append(setup)
            page.append(actions)
            self._refresh_onboarding()
            return page

        def _refresh_onboarding(self) -> None:
            if self.onboarding_status_view is None:
                return
            status = self.controller.onboarding_status()
            self.onboarding_status_view.set_text(status.message)

        def _start_trial(self, *_args: object) -> None:
            status = self.controller.start_trial()
            self.onboarding_status_view.set_text(status.message)
            self.stack.set_visible_child_name("tasks")

        def _start_setup(self, *_args: object) -> None:
            self.stack.set_visible_child_name("account")

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
            experience_title = Gtk.Label(label="Lokales Erfahrungs-Gedächtnis")
            experience_title.set_xalign(0)
            experience_title.add_css_class("section-title")
            page.append(experience_title)
            experience_scroll = Gtk.ScrolledWindow()
            experience_scroll.set_min_content_height(160)
            self.experience_view = Gtk.Label(label="Noch keine Erfahrungen gespeichert.")
            self.experience_view.set_xalign(0)
            self.experience_view.set_yalign(0)
            self.experience_view.set_wrap(True)
            self.experience_view.add_css_class("conversation")
            experience_scroll.set_child(self.experience_view)
            page.append(experience_scroll)
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
            login_title = Gtk.Label(label="Anmelden")
            login_title.set_xalign(0)
            login_title.add_css_class("section-title")
            page.append(login_title)
            login_username = Gtk.Entry()
            login_username.set_placeholder_text("Benutzername")
            page.append(login_username)
            login_password = Gtk.Entry()
            login_password.set_placeholder_text("Passwort")
            login_password.set_visibility(False)
            page.append(login_password)
            login = Gtk.Button(label="Lokal anmelden")
            login.set_halign(Gtk.Align.START)
            login.connect(
                "clicked",
                lambda *_args: self._authenticate_account(
                    login_username, login_password
                ),
            )
            page.append(login)
            username = Gtk.Entry()
            username.set_placeholder_text("Benutzername (3–32 Zeichen)")
            page.append(username)
            password = Gtk.Entry()
            password.set_placeholder_text("Passwort (mindestens 10 Zeichen)")
            password.set_visibility(False)
            page.append(password)
            security_question = Gtk.Entry()
            security_question.set_placeholder_text("Sicherheitsfrage (optional, 3–200 Zeichen)")
            page.append(security_question)
            security_answer = Gtk.Entry()
            security_answer.set_placeholder_text("Antwort auf die Sicherheitsfrage")
            security_answer.set_visibility(False)
            page.append(security_answer)
            create = Gtk.Button(label="Lokales Konto anlegen")
            create.set_halign(Gtk.Align.START)
            create.connect(
                "clicked",
                lambda *_args: self._create_account(
                    username, password, security_question, security_answer
                ),
            )
            page.append(create)
            recovery_title = Gtk.Label(label="Passwort wiederherstellen")
            recovery_title.set_xalign(0)
            recovery_title.add_css_class("section-title")
            page.append(recovery_title)
            recovery_username = Gtk.Entry()
            recovery_username.set_placeholder_text("Benutzername")
            page.append(recovery_username)
            recovery_answer = Gtk.Entry()
            recovery_answer.set_placeholder_text("Antwort auf die Sicherheitsfrage")
            recovery_answer.set_visibility(False)
            page.append(recovery_answer)
            recovery_password = Gtk.Entry()
            recovery_password.set_placeholder_text("Neues Passwort (mindestens 10 Zeichen)")
            recovery_password.set_visibility(False)
            page.append(recovery_password)
            recover = Gtk.Button(label="Passwort zurücksetzen")
            recover.set_halign(Gtk.Align.START)
            recover.connect(
                "clicked",
                lambda *_args: self._reset_account(
                    recovery_username, recovery_answer, recovery_password
                ),
            )
            page.append(recover)
            self.account_status = Gtk.Label(label="Noch kein lokales Konto angelegt.")
            self.account_status.set_xalign(0)
            self.account_status.set_wrap(True)
            self.account_status.add_css_class("muted")
            page.append(self.account_status)
            return page

        def _settings_page(self) -> Any:
            """Build the first-class profile, accessibility, and privacy settings."""
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            page.set_margin_top(24)
            page.set_margin_bottom(24)
            page.set_margin_start(28)
            page.set_margin_end(28)
            title = Gtk.Label(label="Einstellungen")
            title.set_xalign(0)
            title.add_css_class("section-title")
            page.append(title)
            intro = Gtk.Label(
                label=(
                    "Diese Einstellungen werden lokal gespeichert. ACB nutzt sie für "
                    "Antwortstil, Erklärungsniveau, Barrierefreiheit und Bedienung. "
                    "E-Mail, Telefon und Adresse sind nicht erforderlich."
                )
            )
            intro.set_xalign(0)
            intro.set_wrap(True)
            intro.add_css_class("muted")
            page.append(intro)

            scroll = Gtk.ScrolledWindow()
            scroll.set_vexpand(True)
            form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            form.set_margin_top(12)
            form.set_margin_bottom(12)
            form.set_margin_start(4)
            form.set_margin_end(12)

            self._settings_section(form, "Erscheinungsbild & Antworten")
            self._combo_setting(form, "theme", "Darstellung", ("system", "System", "light", "Hell", "dark", "Dunkel"))
            self._combo_setting(
                form,
                "response_style",
                "Antwortumfang",
                ("concise", "Kurz", "balanced", "Ausgewogen", "detailed", "Ausführlich"),
            )
            self._combo_setting(
                form,
                "knowledge_level",
                "Computerkentnisse",
                ("beginner", "Anfänger", "hobbyist", "Hobbyist", "advanced", "Fortgeschritten", "expert", "Erweitert fortgeschritten", "developer", "Entwickler/Developer"),
            )
            self._check_setting(form, "simple_language", "Einfache Sprache verwenden")
            self._check_setting(form, "gendered_language", "Wenn passend gendern")

            self._settings_section(form, "Persönliche Ansprache (optional)")
            self._text_setting(form, "nickname", "Nickname / Alias")
            self._text_setting(form, "pronouns", "Pronomen")
            self._text_setting(form, "gender_identity", "Geschlechtsidentität")
            self._text_setting(form, "interests", "Interessen")
            self._text_setting(form, "occupation", "Beruf / Tätigkeit")
            self._text_setting(form, "age", "Alter", numeric=True)

            self._settings_section(form, "Sprache & Zugänglichkeit")
            self._check_setting(form, "voice_input", "Spracheingabe erlauben")
            self._check_setting(form, "voice_output", "Sprachausgabe erlauben")
            self._check_setting(form, "wake_phrase_enabled", "Individuellen Sprachbefehl aktivieren")
            self._text_setting(form, "wake_phrase", "Sprachbefehl")
            self._check_setting(form, "large_text", "Große Schrift")
            self._check_setting(form, "high_contrast", "Hoher Kontrast")
            self._check_setting(form, "screen_reader", "Screenreader-Unterstützung")
            self._check_setting(form, "braille_input", "Braille-Eingabe")
            self._check_setting(form, "motor_assistance", "Motorische Unterstützung")
            self._check_setting(form, "cognitive_support", "Kognitive Unterstützung")
            self._combo_setting(
                form,
                "color_blind_mode",
                "Farbseh-Unterstützung",
                ("none", "Keine", "red-green", "Rot-Grün", "blue-yellow", "Blau-Gelb", "monochrome", "Monochrom"),
            )

            self._settings_section(form, "Datenschutz & Recherche")
            self._check_setting(form, "store_chat_history", "Chatverlauf lokal speichern")
            self._check_setting(form, "store_task_history", "Aufgabenverlauf lokal speichern")
            self._check_setting(form, "store_personalization", "Personalisierung lokal speichern")
            self._check_setting(form, "allow_network_research", "Vertrauenswürdige Online-Recherche erlauben")
            self._check_setting(form, "require_confirmation_for_sensitive_data", "Vor sensibler Datenfreigabe bestätigen")

            save = Gtk.Button(label="Einstellungen speichern")
            save.set_halign(Gtk.Align.START)
            save.add_css_class("suggested-action")
            save.connect("clicked", self._save_settings)
            form.append(save)
            self.preference_status = Gtk.Label(label="Noch keine Änderungen gespeichert.")
            self.preference_status.set_xalign(0)
            self.preference_status.set_wrap(True)
            self.preference_status.add_css_class("muted")
            form.append(self.preference_status)
            scroll.set_child(form)
            page.append(scroll)
            self._load_settings_controls()
            preferences = self.controller.preferences()
            self._apply_theme(preferences.theme)
            self._apply_accessibility(preferences)
            return page

        @staticmethod
        def _settings_section(form: Any, label: str) -> None:
            heading = Gtk.Label(label=label)
            heading.set_xalign(0)
            heading.set_margin_top(12)
            heading.add_css_class("section-title")
            form.append(heading)

        def _text_setting(self, form: Any, key: str, label: str, *, numeric: bool = False) -> None:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            caption = Gtk.Label(label=label)
            caption.set_xalign(0)
            caption.set_hexpand(True)
            row.append(caption)
            entry = Gtk.Entry()
            entry.set_width_chars(18)
            entry.set_input_purpose(Gtk.InputPurpose.DIGITS if numeric else Gtk.InputPurpose.FREE_FORM)
            row.append(entry)
            form.append(row)
            self.preference_controls[key] = entry

        def _check_setting(self, form: Any, key: str, label: str) -> None:
            check = Gtk.CheckButton(label=label)
            form.append(check)
            self.preference_controls[key] = check

        def _combo_setting(self, form: Any, key: str, label: str, options: tuple[str, ...]) -> None:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            caption = Gtk.Label(label=label)
            caption.set_xalign(0)
            caption.set_hexpand(True)
            row.append(caption)
            combo = Gtk.ComboBoxText()
            for index in range(0, len(options), 2):
                combo.append(options[index], options[index + 1])
            row.append(combo)
            form.append(row)
            self.preference_controls[key] = combo

        def _load_settings_controls(self) -> None:
            preferences = self.controller.preferences()
            for key, control in self.preference_controls.items():
                value = getattr(preferences, key)
                if isinstance(control, Gtk.CheckButton):
                    control.set_active(bool(value))
                elif isinstance(control, Gtk.ComboBoxText):
                    control.set_active_id(str(value))
                else:
                    control.set_text("" if value is None else str(value))

        def _save_settings(self, *_args: object) -> None:
            changes: dict[str, object] = {}
            for key, control in self.preference_controls.items():
                if isinstance(control, Gtk.CheckButton):
                    changes[key] = control.get_active()
                elif isinstance(control, Gtk.ComboBoxText):
                    changes[key] = control.get_active_id() or "system"
                else:
                    raw = control.get_text().strip()
                    changes[key] = int(raw) if key == "age" and raw else (None if key == "age" else raw)
            try:
                preferences = self.controller.update_preferences(changes)
            except (TypeError, ValueError, RuntimeError):
                self.preference_status.set_text("Einstellungen konnten nicht gespeichert werden. Bitte Eingaben prüfen.")
                return
            self._apply_theme(preferences.theme)
            self._apply_accessibility(preferences)
            self.preference_status.set_text(
                "Gespeichert. ACB verwendet diese Einstellungen für neue Antworten und die Bedienung."
            )

        def _apply_theme(self, theme: str) -> None:
            if theme not in THEMES:
                return
            self.window.remove_css_class("theme-light")
            self.window.remove_css_class("theme-dark")
            if theme == "light":
                self.window.add_css_class("theme-light")
            elif theme == "dark":
                self.window.add_css_class("theme-dark")

        def _apply_accessibility(self, preferences: Any) -> None:
            hints = self.controller.assistive_hints()
            for css_class in (
                "large-text",
                "high-contrast",
                "color-red-green",
                "color-blue-yellow",
                "color-monochrome",
            ):
                self.window.remove_css_class(css_class)
            if preferences.large_text or hints.large_text:
                self.window.add_css_class("large-text")
            if preferences.high_contrast or hints.high_contrast:
                self.window.add_css_class("high-contrast")
            mode = preferences.color_blind_mode
            if mode != "none":
                self.window.add_css_class(f"color-{mode}")

        def _header(self) -> Any:
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            bar.add_css_class("topbar")
            brand = Gtk.Label(label="ACB")
            brand.add_css_class("brand")
            bar.append(brand)
            subtitle = Gtk.Label(label="Autonome Computing Butler")
            subtitle.add_css_class("subtitle")
            bar.append(subtitle)
            self.undo_button = Gtk.Button(label="↶ Rückgängig")
            self.undo_button.set_sensitive(False)
            self.undo_button.connect("clicked", self._undo_last)
            bar.append(self.undo_button)
            self.retry_button = Gtk.Button(label="↻ Wiederholen")
            self.retry_button.set_sensitive(False)
            self.retry_button.connect("clicked", self._retry_last)
            bar.append(self.retry_button)
            self.audit_button = Gtk.Button(label="Audit prüfen")
            self.audit_button.connect("clicked", self._show_audit)
            bar.append(self.audit_button)
            terminal_button = Gtk.Button(label="🖥 Protokoll")
            terminal_button.connect("clicked", self._show_terminal_log)
            bar.append(terminal_button)
            export_button = Gtk.Button(label="⇩ Export")
            export_button.connect("clicked", self._export_protocol)
            bar.append(export_button)
            copy_button = Gtk.Button(label="⧉ Kopieren")
            copy_button.connect("clicked", self._copy_current)
            bar.append(copy_button)
            self.speak_button = Gtk.Button(label="🔊 Vorlesen")
            self.speak_button.connect("clicked", self._speak_current)
            bar.append(self.speak_button)
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
            self.current.set_text(
                f"{prefix}: {task.goal}\nStatus: {task.status} · "
                f"{task.progress_percent}% · noch ca. "
                f"{task.estimated_remaining_seconds}s"
            )
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{task.status} · {task.goal}")
            label.set_wrap(True)
            label.set_xalign(0)
            row.set_child(label)
            self.history.prepend(row)

        def _show_persisted_session(self, session: dict[str, object]) -> None:
            if self.history is None:
                return
            status = str(session.get("status", "unknown"))
            goal = str(session.get("original_goal", ""))
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=f"{status} · {goal}")
            label.set_wrap(True)
            label.set_xalign(0)
            row.set_child(label)
            self.history.append(row)

        def _refresh(self) -> bool:
            tasks = self.controller.tasks()
            active = next(
                (item for item in tasks if item.status in {"queued", "running", "recovering"}),
                None,
            )
            if active is None:
                self.send.set_sensitive(True)
                latest_completed = next(
                    (item for item in reversed(self.controller.tasks())
                     if item.status == "completed" and item.session_id),
                    None,
                )
                if self.undo_button is not None:
                    self.undo_button.set_sensitive(latest_completed is not None)
                latest_failed = next(
                    (item for item in reversed(self.controller.tasks())
                     if item.status in {"failed", "rejected"}),
                    None,
                )
                if self.retry_button is not None:
                    self.retry_button.set_sensitive(latest_failed is not None)
            else:
                self.current.set_text(
                    f"{active.goal}\nStatus: {active.status} · "
                    f"{active.progress_percent}% · noch ca. "
                    f"{active.estimated_remaining_seconds}s"
                )
            self._refresh_feedback(tasks)
            self._refresh_learning()
            return True

        def _refresh_feedback(self, tasks: list[UiTask]) -> None:
            completed = next(
                (item for item in reversed(tasks) if item.status == "completed" and item.session_id),
                None,
            )
            if completed is None or completed.session_id is None or self.feedback_box is None:
                return
            if self.feedback_session != completed.session_id:
                self.feedback_session = completed.session_id
                self.feedback_box.set_visible(True)
                self.feedback_status.set_text("Wie bewertest du Ergebnis und Erklärung?")

        def _save_feedback(self, *_args: object) -> None:
            if self.feedback_session is None or self.feedback_rating is None:
                return
            try:
                rating = int(self.feedback_rating.get_active_id() or "10")
                comment = self.feedback_comment.get_text().strip()
                self.controller.add_feedback(self.feedback_session, rating, comment)
            except (TypeError, ValueError, RuntimeError):
                self.feedback_status.set_text("Bewertung konnte nicht gespeichert werden.")
                return
            self.feedback_status.set_text("Danke. Die Bewertung wurde lokal gespeichert.")

        def _undo_last(self, *_args: object) -> None:
            completed = next(
                (item for item in reversed(self.controller.tasks())
                 if item.status == "completed" and item.session_id),
                None,
            )
            if completed is None or completed.session_id is None:
                return
            try:
                result = self.controller.undo(completed.session_id)
            except (TypeError, ValueError, RuntimeError):
                self.current.set_text("Rückgängig nicht verfügbar; der Audit-Status bleibt unverändert.")
                return
            self.current.set_text(
                "Letzte Änderung rückgängig gemacht.\n"
                f"Checkpoint: {result['checkpoint_id']}\n"
                f"Audit: {'gültig' if result['audit_ok'] else 'prüfen'}"
            )

        def _retry_last(self, *_args: object) -> None:
            failed = next(
                (item for item in reversed(self.controller.tasks())
                 if item.status in {"failed", "rejected"}),
                None,
            )
            if failed is None:
                return
            try:
                task = self.controller.retry(failed.request_id)
            except (TypeError, ValueError, RuntimeError):
                self.current.set_text("Wiederholung ist nicht verfügbar.")
                return
            self._show_task(task, recovered=False)

        def _show_audit(self, *_args: object) -> None:
            result = self.controller.audit_status()
            events = self.controller.audit_events(20)
            recent = "\n".join(
                f"#{item['sequence']} {item['event_type']} · {item['created_at']}"
                for item in events[:8]
            )
            self.current.set_text(
                "Audit-Prüfung\n"
                f"Status: {'gültig' if result['ok'] else 'ungültig'}\n"
                f"Sequenz: {result['sequence']}\n"
                f"Code: {result['code']}\n\nLetzte Ereignisse:\n{recent or 'keine'}"
            )

        def _show_terminal_log(self, *_args: object) -> None:
            if self.log_window is not None:
                self.log_window.present()
                return
            self.log_window = Gtk.Window(title="ACB – Terminal und Protokoll")
            self.log_window.set_default_size(900, 560)
            if self.window is not None:
                self.log_window.set_transient_for(self.window)
            self.log_window.connect("close-request", self._close_log_window)
            view = Gtk.TextView()
            view.set_editable(False)
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            lines: list[str] = ["ACB lokales Prozessprotokoll", "=" * 32, ""]
            try:
                for event in self.controller.audit_events(100):
                    lines.append(
                        f"[{event['created_at']}] #{event['sequence']} "
                        f"{event['event_type']} · {event['payload']}"
                    )
            except (TypeError, ValueError, RuntimeError):
                lines.append("Audit-Ereignisse konnten nicht gelesen werden.")
            lines.append("")
            lines.append("Aufgabenstatus")
            lines.append("=" * 16)
            for task in reversed(self.controller.tasks()):
                lines.append(f"[{task.status}] {task.goal}")
                if task.error:
                    lines.append(f"  Fehler: {task.error}")
            view.get_buffer().set_text("\n".join(lines))
            scroll = Gtk.ScrolledWindow()
            scroll.set_child(view)
            self.log_window.set_child(scroll)
            self.log_window.present()

        def _export_protocol(self, *_args: object) -> None:
            try:
                result = self.controller.export_audit_log()
            except (TypeError, ValueError, RuntimeError, OSError):
                self.current.set_text("Protokoll konnte nicht exportiert werden.")
                return
            self.current.set_text(
                "Protokoll exportiert\n"
                f"Datei: {result['path']}\n"
                f"Ereignisse: {result['event_count']}"
            )

        def _copy_current(self, *_args: object) -> None:
            text = self.current.get_text().strip()
            display = Gdk.Display.get_default()
            if not text or display is None:
                return
            display.get_clipboard().set_text(text)

        def _close_log_window(self, *_args: object) -> bool:
            self.log_window = None
            return False

        def _speak_current(self, *_args: object) -> None:
            text = self.current.get_text().strip()
            if not text or (self.voice_thread is not None and self.voice_thread.is_alive()):
                return
            def speak() -> None:
                result = self.controller.speak(text)
                GLib.idle_add(self._show_speech_result, result)

            self.voice_thread = threading.Thread(target=speak, name="acb-voice", daemon=True)
            self.voice_thread.start()

        def _show_speech_result(self, result: Any) -> bool:
            if not result.started:
                self.current.set_text(f"Sprachausgabe nicht gestartet: {result.diagnostic}")
            return False

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
            if self.experience_view is not None:
                experiences = self.controller.learning.store.experiences(30)
                self.experience_view.set_text(
                    "\n\n".join(
                        f"[{item.outcome}] {item.goal}\n{item.lesson}"
                        for item in experiences
                    )
                    or "Noch keine Erfahrungen gespeichert."
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
            if self.research_thread is not None and self.research_thread.is_alive():
                return
            self.research_thread = threading.Thread(
                target=self._run_research,
                name="acb-research-now",
                daemon=True,
            )
            self.research_thread.start()

        def _run_research(self) -> None:
            try:
                self.controller.research_now()
            finally:
                self.research_thread = None

        def _create_account(
            self,
            username: Any,
            password: Any,
            security_question: Any,
            security_answer: Any,
        ) -> None:
            try:
                account = self.controller.create_account(
                    username.get_text().strip(),
                    password.get_text(),
                    security_question=security_question.get_text().strip(),
                    security_answer=security_answer.get_text(),
                )
            except ValueError:
                self.account_status.set_text("Konto konnte nicht angelegt werden. Prüfe Benutzername und Passwort.")
                return
            password.set_text("")
            security_answer.set_text("")
            self.controller.complete_onboarding()
            self.account_status.set_text(
                f"Konto {account.username} lokal angelegt. Geräte-ID: {account.device_id}"
            )
            if self.stack is not None:
                self.stack.set_visible_child_name("tasks")

        def _authenticate_account(self, username: Any, password: Any) -> None:
            account = self.controller.authenticate(
                username.get_text().strip(), password.get_text()
            )
            password.set_text("")
            if account is None:
                self.account_status.set_text("Anmeldung fehlgeschlagen. Prüfe Benutzername und Passwort.")
                return
            self.account_status.set_text(
                f"Lokal angemeldet als {account.username}. Geräte-ID: {account.device_id}"
            )

        def _reset_account(self, username: Any, answer: Any, password: Any) -> None:
            try:
                account = self.controller.reset_password(
                    username.get_text().strip(),
                    answer.get_text(),
                    password.get_text(),
                )
            except (TypeError, ValueError, RuntimeError):
                account = None
            answer.set_text("")
            password.set_text("")
            if account is None:
                self.account_status.set_text(
                    "Passwort konnte nicht zurückgesetzt werden. Prüfe Benutzername, Antwort und Passwortlänge."
                )
            else:
                self.account_status.set_text(
                    f"Passwort für {account.username} wurde lokal neu gesetzt."
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
                .theme-light .app-shell { background: #f7f9fc; color: #172033; }
                .theme-light .topbar, .theme-light .sidebar { background: #e8edf5; }
                .theme-light .conversation, .theme-light .composer { background: #ffffff; }
                .theme-light .section-title { color: #172033; }
                .theme-light .muted, .theme-light .subtitle { color: #53627a; }
                .theme-light entry, .theme-light combobox { background: #ffffff; color: #172033; }
                .theme-dark .app-shell { background: #070c16; }
                .large-text .conversation, .large-text entry, .large-text button { font-size: 21px; }
                .high-contrast .conversation, .high-contrast .composer { border: 2px solid #ffffff; }
                .high-contrast .muted { color: #ffffff; }
                .color-red-green .state-good, .color-red-green .offline { color: #00b7ff; }
                .color-blue-yellow .state-good, .color-blue-yellow .offline { color: #ff7b00; }
                .color-monochrome .app-shell, .color-monochrome .topbar, .color-monochrome .sidebar { background: #111111; color: #ffffff; }
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
