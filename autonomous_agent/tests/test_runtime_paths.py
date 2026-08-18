import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomous_agent.runtime_paths import (
    build_runtime_paths,
    ensure_runtime_directories,
    migrate_legacy_recovery_files,
    migrate_legacy_runtime_files,
)


class RuntimePathsTests(unittest.TestCase):

    def test_builds_isolated_v50_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)

            self.assertEqual(paths.state_file, root / "state/agent_state.json")
            self.assertEqual(
                paths.audit_file,
                root / "state/audit/audit_log.jsonl",
            )
            self.assertEqual(paths.snapshot_root, root / "state/snapshots")
            self.assertEqual(paths.cache_root, root / "runtime/cache")
            self.assertEqual(
                paths.recovery_state_file,
                root / "state/recovery_state.json",
            )
            self.assertEqual(
                paths.recovery_history_file,
                root / "state/snapshots/recovery_history.json",
            )
            self.assertEqual(
                paths.recovery_audit_file,
                root / "state/audit/recovery_audit.json",
            )
            self.assertNotEqual(paths.state_file, paths.recovery_state_file)

    def test_creates_runtime_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = build_runtime_paths(directory)

            ensure_runtime_directories(paths)

            self.assertTrue(paths.state_root.is_dir())
            self.assertTrue(paths.audit_root.is_dir())
            self.assertTrue(paths.snapshot_root.is_dir())
            self.assertTrue(paths.cache_root.is_dir())
            for path in (
                paths.runtime_root,
                paths.state_root,
                paths.audit_root,
                paths.snapshot_root,
                paths.cache_root,
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_migrates_legacy_recovery_files_without_deleting_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            legacy_values = {
                "agent_state.json": "legacy-state",
                "agent_history.json": "legacy-history",
                "agent_audit.json": "legacy-audit",
            }

            for name, value in legacy_values.items():
                (root / name).write_text(value, encoding="utf-8")

            migrated = migrate_legacy_recovery_files(paths, legacy_root=root)

            self.assertEqual(
                migrated,
                (
                    paths.recovery_state_file,
                    paths.recovery_history_file,
                    paths.recovery_audit_file,
                ),
            )
            self.assertEqual(
                paths.recovery_state_file.read_text(encoding="utf-8"),
                "legacy-state",
            )
            self.assertEqual(
                paths.recovery_history_file.read_text(encoding="utf-8"),
                "legacy-history",
            )
            self.assertEqual(
                paths.recovery_audit_file.read_text(encoding="utf-8"),
                "legacy-audit",
            )
            for name, value in legacy_values.items():
                self.assertEqual(
                    (root / name).read_text(encoding="utf-8"),
                    value,
                )

    def test_recovery_migration_does_not_overwrite_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            ensure_runtime_directories(paths)
            (root / "agent_state.json").write_text(
                "legacy",
                encoding="utf-8",
            )
            paths.recovery_state_file.write_text(
                "isolated",
                encoding="utf-8",
            )

            migrated = migrate_legacy_recovery_files(paths, legacy_root=root)

            self.assertNotIn(paths.recovery_state_file, migrated)
            self.assertEqual(
                paths.recovery_state_file.read_text(encoding="utf-8"),
                "isolated",
            )

    def test_recovery_migration_rejects_source_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            target = root / "target.json"
            target.write_text("state", encoding="utf-8")
            (root / "agent_state.json").symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "Symlink"):
                migrate_legacy_recovery_files(paths, legacy_root=root)

    def test_recovery_migration_rejects_destination_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            ensure_runtime_directories(paths)
            target = root / "target.json"
            target.write_text("state", encoding="utf-8")
            paths.recovery_state_file.symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "Symlink"):
                migrate_legacy_recovery_files(paths, legacy_root=root)

    def test_recovery_migration_rejects_non_file_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            ensure_runtime_directories(paths)
            paths.recovery_history_file.mkdir()

            with self.assertRaisesRegex(RuntimeError, "keine Datei"):
                migrate_legacy_recovery_files(paths, legacy_root=root)

    def test_recovery_migration_rejects_non_file_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            (root / "agent_audit.json").mkdir()

            with self.assertRaisesRegex(RuntimeError, "keine Datei"):
                migrate_legacy_recovery_files(paths, legacy_root=root)

    def test_recovery_copy_failure_preserves_source_and_cleans_temporary_file(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            source = root / "agent_state.json"
            source.write_text("rollback-state", encoding="utf-8")

            with patch(
                "autonomous_agent.runtime_paths.shutil.copy2",
                side_effect=OSError("copy failed"),
            ):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    migrate_legacy_recovery_files(paths, legacy_root=root)

            self.assertEqual(
                source.read_text(encoding="utf-8"),
                "rollback-state",
            )
            self.assertFalse(paths.recovery_state_file.exists())
            self.assertEqual(
                list(
                    paths.state_root.glob(
                        ".recovery_state.json.migration.*"
                    )
                ),
                [],
            )

    def test_migrates_legacy_files_without_deleting_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            legacy_values = {
                "agent_state.json": "state",
                "agent_state.json.bak": "backup",
                "agent_state.sha256": "state-hash",
                "audit_log.jsonl": "audit",
                "audit_head.sha256": "audit-head",
            }

            for name, value in legacy_values.items():
                (root / name).write_text(value, encoding="utf-8")

            migrated = migrate_legacy_runtime_files(paths, legacy_root=root)

            self.assertEqual(len(migrated), len(legacy_values))
            self.assertEqual(
                paths.state_file.read_text(encoding="utf-8"),
                "state",
            )
            self.assertEqual(
                paths.audit_file.read_text(encoding="utf-8"),
                "audit",
            )
            for name in legacy_values:
                self.assertTrue((root / name).is_file())

    def test_does_not_overwrite_existing_isolated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            ensure_runtime_directories(paths)
            (root / "agent_state.json").write_text("legacy", encoding="utf-8")
            paths.state_file.write_text("current", encoding="utf-8")

            migrated = migrate_legacy_runtime_files(paths, legacy_root=root)

            self.assertNotIn(paths.state_file, migrated)
            self.assertEqual(
                paths.state_file.read_text(encoding="utf-8"),
                "current",
            )

    def test_rejects_legacy_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            target = root / "target.json"
            target.write_text("state", encoding="utf-8")
            (root / "agent_state.json").symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "Symlink"):
                migrate_legacy_runtime_files(paths, legacy_root=root)

    def test_rejects_runtime_directory_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = build_runtime_paths(root)
            paths.state_root.mkdir()
            target = root / "external-audit"
            target.mkdir()
            paths.audit_root.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "Symlink"):
                ensure_runtime_directories(paths)


if __name__ == "__main__":
    unittest.main()
