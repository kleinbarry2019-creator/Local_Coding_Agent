import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    package_root: Path
    runtime_root: Path
    state_root: Path
    audit_root: Path
    snapshot_root: Path
    cache_root: Path
    state_file: Path
    state_backup: Path
    state_hash: Path
    audit_file: Path
    audit_head: Path
    recovery_state_file: Path
    recovery_history_file: Path
    recovery_audit_file: Path


def build_runtime_paths(package_root):
    package_root = Path(package_root).resolve()
    runtime_root = package_root / "runtime"
    state_root = package_root / "state"
    audit_root = state_root / "audit"

    return RuntimePaths(
        package_root=package_root,
        runtime_root=runtime_root,
        state_root=state_root,
        audit_root=audit_root,
        snapshot_root=state_root / "snapshots",
        cache_root=runtime_root / "cache",
        state_file=state_root / "agent_state.json",
        state_backup=state_root / "agent_state.json.bak",
        state_hash=state_root / "agent_state.sha256",
        audit_file=audit_root / "audit_log.jsonl",
        audit_head=audit_root / "audit_head.sha256",
        recovery_state_file=state_root / "recovery_state.json",
        recovery_history_file=state_root / "snapshots/recovery_history.json",
        recovery_audit_file=audit_root / "recovery_audit.json",
    )


def ensure_runtime_directories(paths):
    for directory in (
        paths.runtime_root,
        paths.state_root,
        paths.audit_root,
        paths.snapshot_root,
        paths.cache_root,
    ):
        if directory.is_symlink():
            raise RuntimeError(
                f"FAIL-CLOSED: Runtime-Verzeichnis ist Symlink: {directory.name}"
            )

        if directory.exists() and not directory.is_dir():
            raise RuntimeError(
                f"FAIL-CLOSED: Runtime-Pfad ist kein Verzeichnis: {directory.name}"
            )

        directory.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )


def _migrate_files(migrations):
    migrated = []

    for source, destination in migrations:
        if source.is_symlink():
            raise RuntimeError(
                f"FAIL-CLOSED: Legacy-Runtime-Datei ist Symlink: {source.name}"
            )

        if destination.is_symlink():
            raise RuntimeError(
                f"FAIL-CLOSED: Runtime-Zieldatei ist Symlink: {destination.name}"
            )

        if destination.exists():
            if not destination.is_file():
                raise RuntimeError(
                    "FAIL-CLOSED: Runtime-Ziel ist keine Datei: "
                    f"{destination.name}"
                )
            continue

        if not source.exists():
            continue

        if not source.is_file():
            raise RuntimeError(
                f"FAIL-CLOSED: Legacy-Runtime-Pfad ist keine Datei: {source.name}"
            )

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.migration.",
            dir=str(destination.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)

        try:
            shutil.copy2(
                source,
                temporary,
                follow_symlinks=False,
            )
            try:
                os.link(
                    temporary,
                    destination,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file():
                    raise RuntimeError(
                        "FAIL-CLOSED: Runtime-Ziel ist keine Datei: "
                        f"{destination.name}"
                    )
                continue
        finally:
            if temporary.exists():
                temporary.unlink()

        migrated.append(destination)

    return tuple(migrated)


def migrate_legacy_runtime_files(paths, legacy_root=None):
    """Copy legacy V50 runtime files once without overwriting new storage."""

    legacy_root = Path(legacy_root or paths.package_root).resolve()
    ensure_runtime_directories(paths)

    migrations = (
        (legacy_root / "agent_state.json", paths.state_file),
        (legacy_root / "agent_state.json.bak", paths.state_backup),
        (legacy_root / "agent_state.sha256", paths.state_hash),
        (legacy_root / "audit_log.jsonl", paths.audit_file),
        (legacy_root / "audit_head.sha256", paths.audit_head),
    )
    return _migrate_files(migrations)


def migrate_legacy_recovery_files(paths, legacy_root=None):
    """Copy modular recovery files once without replacing isolated storage."""

    legacy_root = Path(legacy_root or Path.cwd()).resolve()
    ensure_runtime_directories(paths)
    migrations = (
        (legacy_root / "agent_state.json", paths.recovery_state_file),
        (legacy_root / "agent_history.json", paths.recovery_history_file),
        (legacy_root / "agent_audit.json", paths.recovery_audit_file),
    )
    return _migrate_files(migrations)
