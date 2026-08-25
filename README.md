# ACB – Autonome Computing Butler

ACB (Autonome Computing Butler) is a free, local-first coding and system agent. It combines
the hardened V50 sandbox with the modular policy, typed-tool, persistent-state,
audit, recovery, doctor, and Ollama foundation. Existing V50 behavior remains
available while the installable CLI owns the consolidated runtime path.

## Requirements

On Bazzite or another Linux distribution, install these free prerequisites:

- Python 3.12, 3.13, or 3.14;
- [uv](https://docs.astral.sh/uv/);
- Git;
- a local [Ollama](https://ollama.com/) installation.

No API key, cloud account, subscription, trial, or payment method is required.

## Development setup

Clone the repository, enter it, and create the reproducible development
environment:

```bash
uv sync --frozen --group dev
uv run acb doctor
```

For a user-level `acb` command, install the checked-out package with uv:

```bash
uv tool install .
acb doctor
```

The default human report is grouped into system, development, local model,
containment, and policy sections. A typical abbreviated result looks like:

```text
ACB – Autonome Computing Butler doctor: warning
System:
  [PASS] doctor.python: Python is supported.
Development:
  [PASS] doctor.git: Git is available.
Local model:
  [WARNING] doctor.ollama-health: Ollama is unavailable.
Containment:
Policy:
  [PASS] mode: monitored
  [PASS] free-only: enabled
```

Automation can request the deterministic schema-version-1 JSON document:

```bash
acb doctor --json
python3 -m autonomous_agent doctor --json
```

## Autonomous tasks

Simple German or English requests can be executed in autonomous mode. Every
request is normalized into explicit acceptance criteria, persisted in the core
SQLite state, executed through the shared policy/tool boundary, and then
independently rechecked:

```bash
acb run 'Erstelle `result.txt` mit dem Inhalt `verified`'
acb run 'Run python3 -m pytest' --json
acb resume session-0123456789abcdef
```

Supported deterministic intents are file write/read/list, sandboxed process
execution, and allowlisted external-tool installation. Processes run without a
shell in a networkless Bubblewrap sandbox. Missing executable capabilities are
detected automatically. Installation is restricted to a fixed tool/package
catalog, uses a short-lived action digest, spawns at most one non-interactive
privileged child when needed, and verifies the installed executable/version.
The agent process itself refuses to act as a permanent root process.

`completed` is emitted only when every derived acceptance criterion passes,
the action was really executed, and a public-boundary E2E recheck matches the
original request. A successful process exit by itself is only one criterion.

The problem-solving trace classifies failures by cause, records bounded
evidence, and lists safe recovery strategies in the result field
`problem_solving`. When a `command not found` error is detected, ACB first
verifies or provisions the trusted capability and retries the affected step.
Mutating steps are rolled back through checkpoints before retries. The final
evaluator uses the last verified observation of a repaired step, while failed
intermediate attempts remain available for diagnosis.

Vor jedem Auftrag prüft ein Plan-Preflight außerdem die Abhängigkeiten als
Graph, erkennt doppelte Schritte, unbekannte Abhängigkeiten, Zyklen,
Symlink-Ziele und Ziele außerhalb des Projektbereichs. Risiko, Reihenfolge und
eventuelle Blocker werden im Feld `plan_assessment` mit dem Auftrag gespeichert;
ein ungültiger Plan wird vor dem ersten Tool-Aufruf sicher beendet.

Für Coding-Aufträge kann ACB ein Projekt lokal und offline vermessen. Der
gemeinsame Read-only-Tool `project.analyze` erkennt bounded die vorhandenen
Programmiersprachen, Projekt-/Build-Manifeste, Testkonfigurationen, Datei- und
Verzeichnisstruktur sowie testbare nächste Hinweise. Die Analyse liest keine
externen Quellen und führt keinen gefundenen Code aus; sie liefert nur
verifizierbare Grundlage für die anschließende Planung.

Die authentifizierte lokale Schnittstelle `/api/capabilities` liefert außerdem
die zentrale Capability-Matrix mit Status (`available`, `conditional` oder
`gated`), Voraussetzungen und Grenzen. Damit kann die GUI einem Benutzer
verständlich anzeigen, welche Coding-, Lern-, Recovery-, Security-, Sprach- und
Kompatibilitätsfunktionen auf dem aktuellen System tatsächlich nutzbar sind.

## Grafische Oberfläche

ACB enthält eine lokale Browser-Oberfläche für Aufträge, Status, Sessions und
Acceptance-Criteria. Sie verwendet exakt denselben Autonomy-Runtime-, State-
und Recovery-Layer wie die CLI und bindet ausschließlich an Loopback:

```bash
acb ui --project /absolute/path/to/project --open
```

Ohne `--open` zeigt ACB die lokale Adresse an; sie kann dann im Browser geöffnet
werden. Der Standard ist `http://127.0.0.1:8765/`. Jeder Schreibauftrag benötigt
ein zufälliges Sitzungstoken, das nur der lokal ausgelieferten Oberfläche bekannt
ist. Netzwerkzugriff auf die UI wird nicht akzeptiert.

## Lokale Desktop-App und Offline-Fortsetzung

Für die installierte Anwendung gibt es zusätzlich eine native GTK-Desktop-App:

```bash
uv tool install .
acb app
```

Für einen Eintrag im lokalen Anwendungsmenü kann die mitgelieferte Desktop-
Definition auch direkt über ACB installiert werden:

```bash
acb install-desktop
```

Der Eintrag wird ausschließlich im Benutzerkonto unter
`~/.local/share/applications/acb.desktop` angelegt; Root-Rechte und Netzwerk sind
nicht erforderlich.

Die Desktop-App öffnet keinen Netzwerkdienst. Auf Linux wird sie mit dem lokalen
GTK-System gestartet, zeigt den Arbeitsbereich und den Aufgabenverlauf an und
setzt beim Neustart alle persistent als `pending`, `running` oder `recovering`
gespeicherten Aufgaben automatisch fort. Die Runtime verwendet nur lokale
Werkzeuge und den lokalen State; externe Netzwerkverbindungen sind für die App
nicht erforderlich.

Die App benötigt GTK 4 und PyGObject auf dem lokalen System. Falls diese
Desktop-Integration nicht vorhanden ist, bleibt `acb ui` als sichere lokale
Browser-Oberfläche verfügbar.

### Lernen, Recherche und kontrollierte Selbstentwicklung

ACB besitzt in der Desktop-App eigene Reiter für `Wissen & Recherche`,
`Verbesserungen`, `Selbstentwicklung` sowie `Konto & Sync`. Ein lokaler
Scheduler prüft in regelmäßigen Abständen allow-listete HTTPS-Feeds zu KI- und
Sicherheitsmeldungen (arXiv, NIST und CISA). Ohne Netz bleibt der Scheduler
lokal verfügbar und meldet den Offline-Zustand; gespeichertes Wissen,
Auftragsnachprüfungen und Vorschläge bleiben nutzbar.

Neue Informationen werden als lokale Wissenseinträge und nachvollziehbare
Verbesserungsvorschläge abgelegt. Ein externer Artikel kann niemals direkt Code
ausführen. Eine mögliche Änderung wird erst nach Ressourcenprüfung,
Acceptance-Tests, Security-Scan, Release-Gate und Checkpoint/Rollback zur
Übernahme zugelassen. Damit kann ACB kontinuierlich lernen und konkrete
Verbesserungen vorbereiten, ohne die Sicherheitsgrenzen des Systems zu
überspringen.

Abgeschlossene Aufgaben erzeugen automatisch eine spätere Review-Notiz, zum
Beispiel für Regressionstests, Dokumentation oder sicherere Automatisierung.
Auch fehlgeschlagene oder unvollständige Aufträge werden mit den nicht
erfüllten Akzeptanzkriterien als hoch priorisierte Lernvorschläge erfasst.
Wiederholt fehlgeschlagene Kriterien werden zu einem gemeinsamen
Root-Cause-/Regressionstest-Vorschlag verdichtet.
Eine Nutzerbewertung erzeugt zusätzlich einen nachvollziehbaren Vorschlag zur
Verbesserung; alle diese Vorschläge bleiben zunächst nur Kandidaten und
benötigen vor jeder Übernahme Tests, Security-Scan, Release-Gate und Rollback.
Wiederholte niedrige Bewertungen werden dem aktiven lokalen Benutzerkonto bzw.
dem lokalen Profil zugeordnet und in einen eigenen Qualitäts-/Regressionstest-
Vorschlag überführt. So kann ACB Antwortstil und Hilfestellung individuell
weiterentwickeln. In `Einstellungen > Datenschutz & Recherche` lässt sich die
automatische Anpassung des Antwortstils aus diesem Feedback unabhängig von der
lokalen Feedback-/Chatablage deaktivieren. Im Reiter `Selbstentwicklung` kann
das aktive persönliche Antwort-Lernprofil jederzeit zurückgesetzt werden; die
ursprünglichen Aufgaben- und Chat-Audits bleiben dabei erhalten.
Gespeichertes Wissen wird für neue Ziele lokal und erklärbar nach Relevanz
sortiert und kann über die authentifizierte Schnittstelle
`/api/learning/context` als Referenzkontext abgerufen werden. Die Inhalte
werden dabei nie als Befehle interpretiert.
Lokale Konten werden mit scrypt-Passwort-Hashes gespeichert. Die Geräte- und
Kontostruktur für eine spätere system- und netzwerkübergreifende Synchronisation
ist vorbereitet; Passwörter und Geheimnisse werden niemals in ein Sync-Mani-
fest exportiert und es wird keine Cloud-Verbindung ohne ausdrückliches Pairing
angelegt. Die Recherche kann für eine Sitzung über `ACB_RESEARCH_NETWORK=0`
abgeschaltet werden.

### Profil, Darstellung und Bedienung

Der Reiter `Einstellungen` speichert ein lokales Profil unter dem geschützten
State-Verzeichnis. Dort können Darstellung (`System`, `Hell`, `Dunkel`),
Antwortumfang, einfache Sprache, Gendern, Nickname/Pronomen, optionale
Interessen und Beruf sowie das eigene Wissensniveau eingestellt werden.
Zusätzlich gibt es Schalter für Sprach-Ein-/Ausgabe, einen eigenen Wake-Satz,
große Schrift, hohen Kontrast, Screenreader-/Braille-Unterstützung,
motorische/kognitive Unterstützung und Farbsehmodi. Datenschutz- und
Recherchefreigaben sind getrennt steuerbar; standardmäßig bleibt die Freigabe
für sensible Daten bestätigungspflichtig.

Die gleichen Präferenzen können über die authentifizierte lokale UI-Schnittstelle
`/api/preferences` gelesen und als JSON geändert werden. Dadurch können spätere
Chat-, Sprach- und mobile Adapter dieselbe geprüfte Profilstruktur verwenden,
ohne eine zweite Einstellungslogik einzuführen.

Sprachmodelle, gerätespezifische Mikrofon-/Lautsprecheradapter und eine
geräteübergreifende Synchronisierung bleiben bewusst optionale Adapter. Ohne
vorhandene lokale Sprach- oder Assistenzhardware fällt ACB sicher auf Tastatur,
Standardaudio und die gewählte Darstellung zurück; es werden keine fremden
Treiber oder externen Konten automatisch installiert.

Der Erststart bietet jetzt eine lokale dreitägige Testphase oder die direkte
Profileinrichtung. Testaufträge bleiben in dieser Phase eingeschränkt. Die
Runtime kann außerdem den Antwortkontext (Wissensniveau, einfache Sprache,
Ansprache und nächste Schritte), lokale Assistenzhinweise und verfügbare
Offline-Sprachadapter melden. Nach einem abgeschlossenen Auftrag kann eine
Bewertung von 1 bis 10 mit Kommentar lokal gespeichert werden; sie verändert
keinen Audit- oder Task-State.

Die Desktop-Kopfzeile bietet zusätzlich `Audit prüfen` und `Rückgängig`. Die
Rückgängig-Funktion verwendet die vorhandenen, verifizierten Checkpoints der
Runtime und kann bis zu fünf noch verfügbare Mutationsschritte eines Auftrags
einzeln zurückrollen. Jeder Rückrollvorgang wird erneut gegen die Audit-Kette
geprüft; ein unklarer oder beschädigter Checkpoint wird nicht angewendet.

Example (shown on multiple lines here only for readability):

```json
{
  "free_only": true,
  "generated_at": "2026-08-24T00:00:00Z",
  "mode": "monitored",
  "probes": [
    {
      "code": "doctor.ollama-health.unavailable",
      "data": {},
      "duration_ms": 1,
      "name": "doctor.ollama-health",
      "required": false,
      "status": "warning",
      "summary": "Ollama is unavailable.",
      "truncated": false
    }
  ],
  "project_root": "/path/to/project",
  "schema_version": 1,
  "status": "warning"
}
```

The JSON command writes exactly one document to standard output. Exit status 0
means healthy, 1 means warning or unhealthy, 2 means invalid command or
configuration, and 3 means an unexpected internal diagnostic failure.

## Modes and safety boundaries

`monitored` is the safe default. The diagnostic policy can be selected
explicitly for a future autonomous session without enabling agent execution:

```bash
acb doctor --mode autonomous
```

There is no unrestricted-root execution mode and `acb doctor` never executes
coding tasks. The free-only invariant is immutable:
runtime model checks stay local through Ollama, and paid, metered, trial, or
charge-capable providers cannot be enabled by configuration.

Doctor is read-only and zero-write. It does not create configuration or state
directories, SQLite/WAL files, sessions, or audit events. Its default paths are:

- configuration: `$XDG_CONFIG_HOME/local-coding-agent/config.toml`, falling
  back to `$HOME/.config/local-coding-agent/config.toml`;
- state: `$XDG_STATE_HOME/local-coding-agent`, falling back to
  `$HOME/.local/state/local-coding-agent`.

An explicit absolute project can be inspected with `--project PATH`; an
existing or future absolute state location can be inspected with `--state-dir
PATH`. The paths are validated but never created by doctor.

The `agent` command remains available as a compatibility alias for existing
installations; `acb` is the canonical command and display name.

## Compatibility and design

Legacy `safe_agent_v50.py`, its state, recovery behavior, tests, and release
artifacts remain compatible. The active installable CLI uses the modular core;
legacy scripts are compatibility surfaces, not a second active runtime.

The current contracts and rollout are documented in the
[phase-1 design](docs/superpowers/specs/2026-08-18-core-foundation-design.md) and
[implementation plan](docs/superpowers/plans/2026-08-18-core-foundation.md).
