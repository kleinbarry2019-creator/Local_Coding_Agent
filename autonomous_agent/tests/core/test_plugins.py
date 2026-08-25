from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autonomous_agent.core.plugins import PluginCatalog


def _write_manifest(root: Path, *, digest: str, **changes: object) -> None:
    plugin_dir = root / ".acb" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "demo.py").write_text("def register(): return None\n", encoding="utf-8")
    document: dict[str, object] = {
        "plugin_id": "demo.tool",
        "version": "1.0.0",
        "entrypoint": "demo.py",
        "capabilities": ["project.read"],
        "permissions": ["project.read", "network.none"],
        "sha256": digest,
    }
    document.update(changes)
    (plugin_dir / "demo.json").write_text(json.dumps(document), encoding="utf-8")


def test_catalog_verifies_entrypoint_digest_without_importing_code(tmp_path: Path) -> None:
    source = b"def register(): return None\n"
    digest = hashlib.sha256(source).hexdigest()
    _write_manifest(tmp_path, digest=digest)

    records = PluginCatalog(tmp_path).scan()

    assert len(records) == 1
    assert records[0].status == "verified"
    assert records[0].plugin_id == "demo.tool"
    assert records[0].entrypoint == "demo.py"


def test_catalog_rejects_digest_mismatch_and_unsupported_permissions(tmp_path: Path) -> None:
    _write_manifest(tmp_path, digest="0" * 64, permissions=["network.internet"])

    record = PluginCatalog(tmp_path).scan()[0]

    assert record.status == "rejected"
    assert record.reason


def test_catalog_rejects_entrypoint_escape(tmp_path: Path) -> None:
    source = b"def register(): return None\n"
    _write_manifest(
        tmp_path,
        digest=hashlib.sha256(source).hexdigest(),
        entrypoint="../outside.py",
    )

    record = PluginCatalog(tmp_path).scan()[0]

    assert record.status == "rejected"
    assert "entrypoint" in record.reason
