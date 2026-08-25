"""Explainable capability inventory shared by UI, CLI, and future remotes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class CapabilityEntry:
    capability_id: str
    category: str
    status: str
    description: str
    limits: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.capability_id,
            "category": self.category,
            "status": self.status,
            "description": self.description,
            "limits": list(self.limits),
            "prerequisites": list(self.prerequisites),
        }


def capability_matrix() -> dict[str, object]:
    """Return the bounded, user-facing inventory of integrated capabilities."""
    entries = (
        CapabilityEntry(
            "coding.autonomous", "coding", "available",
            "Normalisiert, plant, führt aus, testet und verifiziert Coding-Aufträge.",
            ("Nur freigegebene lokale Tools und Projektpfade.",),
        ),
        CapabilityEntry(
            "coding.project-analysis", "coding", "available",
            "Erkennt Projektstruktur, Programmiersprachen, Architektur, Manifeste und einen Testplan.",
            ("Bounded read-only Analyse; kein Code wird dabei ausgeführt.",),
        ),
        CapabilityEntry(
            "extensibility.secure-plugins", "extensibility", "conditional",
            "Prüft lokale Plugin-Manifeste, Berechtigungen und Entry-Point-Digests.",
            ("Nur verifizierte Metadaten werden angezeigt; Plugin-Code wird nicht automatisch geladen.",),
            ("Manifest unter .acb/plugins",),
        ),
        CapabilityEntry(
            "coding.sandboxed-process", "coding", "available",
            "Führt Prozesse ohne Shell in einer netzwerkfreien Sandbox aus.",
            ("Bubblewrap und lokale Systemwerkzeuge erforderlich.",),
        ),
        CapabilityEntry(
            "learning.local-memory", "learning", "available",
            "Speichert Wissen, Erfahrungen, Feedback und nachvollziehbare Lernhinweise lokal.",
            ("State bleibt bounded und benutzerkontrolliert.",),
        ),
        CapabilityEntry(
            "learning.trusted-research", "learning", "gated",
            "Recherchiert allow-listete KI- und Sicherheitsquellen und erstellt Vorschläge.",
            ("HTTPS-Allowlist, Größenlimits und XML-Sicherheitsprüfung.",),
            ("Netzwerkpräferenz aktiviert",),
        ),
        CapabilityEntry(
            "learning.self-update", "learning", "gated",
            "Bereitet selbstständige Verbesserungen vor und übernimmt sie nur nach Gates.",
            ("Tests, Security, Release und Rollback-Evidenz sind zwingend.",),
        ),
        CapabilityEntry(
            "learning.capability-research", "learning", "available",
            "Erforscht fehlende lokale Capabilities, wählt nur verifizierte Nachrüstungen und startet danach einen begrenzten Retry.",
            ("Immutable Hosts, fehlende Medien, Reboots und Lizenzbedingungen bleiben explizite externe Grenzen.",),
        ),
        CapabilityEntry(
            "learning.goal-research", "learning", "available",
            "Erforscht unbekannte komplexe Aufgaben browserlos aus dem lokalen Capability-Bestand und erzeugt einen bounded plan.",
            ("Ohne passende Implementierungs- und E2E-Fähigkeit wird keine Fertigstellung behauptet.",),
        ),
        CapabilityEntry(
            "recovery.crash-resume", "recovery", "available",
            "Setzt pending-, running- und recovering-Aufträge nach Neustarts fort.",
            ("Persistenter lokaler State erforderlich.",),
        ),
        CapabilityEntry(
            "recovery.rollback", "recovery", "available",
            "Erstellt Checkpoints und ermöglicht bis zu fünf geprüfte Rückrollschritte.",
            ("Nur unterstützte mutierende Schritte.",),
        ),
        CapabilityEntry(
            "security.policy-audit", "security", "available",
            "Erzwingt Tool-, Pfad-, Netzwerk- und Berechtigungsgrenzen mit Audit-Kette.",
            ("Keine dauerhafte Root-Ausführung.",),
        ),
        CapabilityEntry(
            "security.remote-control", "connectivity", "gated",
            "Lokale Browser- und Desktop-Oberfläche; Remote-Zugriff ist vorbereitet.",
            ("Kein offener Netzwerkdienst ohne späteres explizites Pairing.",),
        ),
        CapabilityEntry(
            "virtualization.windows-vm-preflight", "virtualization", "available",
            "Prüft Hypervisor, KVM, Installationsmedium und GPU-Passthrough-Voraussetzungen, ohne den Host zu verändern.",
            ("Die VM-Erstellung bleibt bis zu verifizierten Medien, Ressourcen und einem sicheren Gast-Test unvollständig.",),
        ),
        CapabilityEntry(
            "interface.desktop-browser", "interface", "available",
            "GTK-Desktop-App und loopback-gebundene Browser-Oberfläche.",
            ("GTK 4 ist für die native App erforderlich.",),
        ),
        CapabilityEntry(
            "interface.voice-accessibility", "interface", "conditional",
            "Lokale Sprach- und Accessibility-Erkennung gemäß Systemfähigkeiten.",
            ("Verfügbare lokale Geräte und Adapter bestimmen den Umfang.",),
        ),
        CapabilityEntry(
            "compatibility.system-profile", "compatibility", "available",
            "Doctor, Ressourcenlimits und sichere Pfadprüfung passen die Ausführung an.",
            ("Nicht unterstützte Hardware wird nicht automatisch verändert.",),
        ),
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "capabilities": [entry.to_dict() for entry in entries],
    }


__all__ = ["CapabilityEntry", "capability_matrix"]
