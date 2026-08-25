"""Verified, metadata-only plugin catalog for future tool extensions.

The catalog deliberately does not import or execute plugin code.  A manifest
must describe a project-local entrypoint, its requested capabilities, and the
SHA-256 digest of that entrypoint.  Execution remains subject to the shared
typed tool registry and policy boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path

_MAX_MANIFEST_BYTES = 65_536
_MAX_PLUGINS = 64
_MAX_LIST_ITEMS = 32
_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_PERMISSIONS = frozenset(
    {"project.read", "project.write", "process.run", "network.none"}
)
_REQUIRED_FIELDS = frozenset(
    {"plugin_id", "version", "entrypoint", "capabilities", "permissions", "sha256"}
)


@dataclass(frozen=True)
class PluginRecord:
    plugin_id: str
    version: str
    manifest_path: str
    entrypoint: str | None
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...]
    sha256: str | None
    status: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.plugin_id,
            "version": self.version,
            "manifest": self.manifest_path,
            "entrypoint": self.entrypoint,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "sha256": self.sha256,
            "status": self.status,
            "reason": self.reason,
        }


class PluginCatalog:
    """Scan project-local manifests without loading arbitrary code."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve(strict=True)
        if not self.project_root.is_dir():
            raise ValueError("project root is not a directory")
        self.plugin_root = self.project_root / ".acb" / "plugins"

    def scan(self) -> tuple[PluginRecord, ...]:
        container = self.plugin_root.parent
        try:
            if container.is_symlink() or self.plugin_root.is_symlink():
                return (
                    self._rejected(
                        ".acb/plugins", "plugin directory symlink is not trusted"
                    ),
                )
        except OSError:
            return (self._rejected(".acb/plugins", "plugin directory is unreadable"),)
        if not self.plugin_root.exists():
            return ()
        if not self.plugin_root.is_dir():
            return (self._rejected("plugins", "plugin directory is not a directory"),)
        records: list[PluginRecord] = []
        for manifest in sorted(self.plugin_root.glob("*.json"))[:_MAX_PLUGINS]:
            if manifest.is_symlink() or not manifest.is_file():
                continue
            records.append(self._verify(manifest))
        return tuple(records)

    def _verify(self, manifest: Path) -> PluginRecord:
        relative_manifest = manifest.relative_to(self.project_root).as_posix()
        try:
            payload = manifest.read_bytes()
            if len(payload) > _MAX_MANIFEST_BYTES:
                raise ValueError("manifest exceeds size limit")
            document = json.loads(payload.decode("utf-8"))
            if type(document) is not dict or set(document) != _REQUIRED_FIELDS:
                raise ValueError("manifest fields are incomplete or unsupported")
            plugin_id = _text(document["plugin_id"], "plugin_id", _PLUGIN_ID)
            version = _text(document["version"], "version", _SEMVER)
            entrypoint = _entrypoint(document["entrypoint"])
            capabilities = _items(document["capabilities"], "capabilities")
            permissions = _items(document["permissions"], "permissions")
            if any(item not in _ALLOWED_PERMISSIONS for item in permissions):
                raise ValueError("manifest requests an unsupported permission")
            digest = document["sha256"]
            if type(digest) is not str or _SHA256.fullmatch(digest) is None:
                raise ValueError("sha256 must be a lowercase SHA-256 digest")
            target = manifest.parent / entrypoint
            if target.is_symlink() or not target.is_file():
                raise ValueError("entrypoint is missing or is a symlink")
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(manifest.parent.resolve(strict=True)):
                raise ValueError("entrypoint escapes the plugin directory")
            actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if not hmac.compare_digest(actual, digest):
                raise ValueError("entrypoint digest does not match manifest")
            return PluginRecord(
                plugin_id,
                version,
                relative_manifest,
                entrypoint,
                tuple(capabilities),
                tuple(permissions),
                digest,
                "verified",
                "manifest-and-entrypoint-verified",
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            return self._rejected(relative_manifest, str(error)[:160])

    @staticmethod
    def _rejected(manifest: str, reason: str) -> PluginRecord:
        return PluginRecord("", "", manifest, None, (), (), None, "rejected", reason)


def _text(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or len(value) > 128 or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _entrypoint(value: object) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError("entrypoint is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.name.startswith("."):
        raise ValueError("entrypoint must stay inside the plugin directory")
    return path.as_posix()


def _items(value: object, field: str) -> list[str]:
    if type(value) is not list or len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"{field} must be a bounded list")
    result = []
    for item in value:
        if type(item) is not str or not item or len(item) > 128:
            raise ValueError(f"{field} contains an invalid item")
        result.append(item)
    return list(dict.fromkeys(result))


__all__ = ["PluginCatalog", "PluginRecord"]
