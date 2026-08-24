import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomous_agent import state_manager as state_manager_module
from autonomous_agent.recovery_authority import RecoveryAuthority
from autonomous_agent.recovery_gate import RecoveryGate
from autonomous_agent.recovery_governance import RecoveryGovernanceEngine
from autonomous_agent.state_manager import StateManager


class RecoveryStorageTests(unittest.TestCase):

    def default_manager(self, package_root, legacy_root):
        with (
            patch.object(state_manager_module, "PACKAGE_ROOT", package_root),
            patch.object(
                state_manager_module.Path,
                "cwd",
                return_value=legacy_root,
            ),
        ):
            return StateManager()

    def test_default_manager_uses_isolated_recovery_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            manager = self.default_manager(package_root, root)

            self.assertEqual(
                Path(manager.path),
                package_root / "state/recovery_state.json",
            )
            self.assertEqual(
                Path(manager.history_path),
                package_root / "state/snapshots/recovery_history.json",
            )
            self.assertEqual(
                Path(manager.audit_path),
                package_root / "state/audit/recovery_audit.json",
            )

    def test_default_manager_migrates_all_legacy_files_and_keeps_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            legacy = {
                "agent_state.json": {
                    "schema_version": 1,
                    "status": "legacy",
                },
                "agent_history.json": [],
                "agent_audit.json": [{"event": "legacy"}],
            }
            for name, value in legacy.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")

            manager = self.default_manager(package_root, root)

            self.assertEqual(manager.state["status"], "legacy")
            self.assertEqual(
                json.loads(
                    Path(manager.history_path).read_text(encoding="utf-8")
                ),
                [],
            )
            self.assertEqual(
                json.loads(
                    Path(manager.audit_path).read_text(encoding="utf-8")
                ),
                [{"event": "legacy"}],
            )
            for name, value in legacy.items():
                self.assertEqual(
                    json.loads((root / name).read_text(encoding="utf-8")),
                    value,
                )

    def test_existing_isolated_state_is_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            isolated = package_root / "state/recovery_state.json"
            isolated.parent.mkdir(parents=True)
            isolated.write_text(
                json.dumps({"schema_version": 1, "status": "isolated"}),
                encoding="utf-8",
            )
            (root / "agent_state.json").write_text(
                json.dumps({"schema_version": 1, "status": "legacy"}),
                encoding="utf-8",
            )

            manager = self.default_manager(package_root, root)

            self.assertEqual(manager.state["status"], "isolated")

    def test_explicit_path_derives_companions_and_bypasses_default_migration(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            custom_root = root / "embedded"
            custom_root.mkdir()
            custom_state = custom_root / "custom_state.json"
            (root / "agent_state.json").write_text(
                json.dumps({"schema_version": 1, "status": "legacy"}),
                encoding="utf-8",
            )

            with patch.object(
                state_manager_module,
                "PACKAGE_ROOT",
                package_root,
            ):
                manager = StateManager(custom_state)

            self.assertFalse((package_root / "state").exists())
            self.assertEqual(Path(manager.path), custom_state)
            self.assertEqual(
                Path(manager.history_path),
                custom_root / "agent_history.json",
            )
            self.assertEqual(
                Path(manager.audit_path),
                custom_root / "agent_audit.json",
            )

    def test_explicit_companion_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            history = root / "history/custom.json"
            audit = root / "audit/custom.json"

            manager = StateManager(state, history, audit)

            self.assertEqual(Path(manager.history_path), history)
            self.assertEqual(Path(manager.audit_path), audit)

    def test_valid_isolated_state_retains_auto_recovery_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            manager = self.default_manager(package_root, root)
            manager.state = {
                "schema_version": 1,
                "status": "completed",
            }
            decision = {
                "action": "continue",
                "reason": "state_healthy",
            }
            governance = RecoveryGovernanceEngine(manager)
            authorization = governance.authorize(decision)
            manager.state["recovery_confidence"] = authorization["confidence"]
            decision["confidence"] = authorization["confidence"]
            allowed = RecoveryGate(governance).authorize_recovery(decision)

            result = RecoveryAuthority(
                manager,
                decision,
                allowed,
            ).evaluate()

            self.assertTrue(allowed)
            self.assertEqual(result["authority"], "AUTO_RECOVERY")
            self.assertFalse(result["execution_blocked"])

    def test_invalid_isolated_schema_restores_last_trusted_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            manager = self.default_manager(package_root, root)
            manager.state = {
                "schema_version": 1,
                "status": "completed",
                "recovery_event": False,
            }
            trusted = manager.save_snapshot("known-good")
            manager.state["corrupted_test"] = True
            manager.save_snapshot("schema-invalid")
            manager.state.pop("corrupted_test")
            manager.state["status"] = "valid-looking-descendant"
            manager.save_snapshot("after-schema-invalid")
            Path(manager.path).write_text(
                json.dumps({"schema_version": 999, "status": "completed"}),
                encoding="utf-8",
            )

            recovered = self.default_manager(package_root, root)
            result = recovered.validate_or_recover()

            self.assertTrue(result["accepted"])
            self.assertTrue(result["recovered"])
            self.assertEqual(result["snapshot"]["id"], trusted["id"])
            self.assertEqual(recovered.state["status"], "completed")
            self.assertTrue(recovered.state["recovery_event"])
            audit = json.loads(
                Path(recovered.audit_path).read_text(encoding="utf-8")
            )
            self.assertEqual(audit[-2]["event"], "state_schema_violation")
            self.assertEqual(
                audit[-2]["errors"],
                ["schema_version.unsupported"],
            )
            self.assertNotIn("completed", json.dumps(audit[-2]))
            self.assertEqual(audit[-1]["event"], "state_recovery")
            self.assertFalse((root / "agent_audit.json").exists())

    def test_malformed_isolated_state_is_not_treated_as_healthy_empty_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            manager = self.default_manager(package_root, root)
            Path(manager.path).write_text("{broken-json", encoding="utf-8")

            recovered = self.default_manager(package_root, root)
            result = recovered.validate_or_recover()

            self.assertFalse(result["accepted"])
            self.assertFalse(result["recovered"])
            self.assertEqual(result["errors"], ["state.unreadable"])
            audit = json.loads(
                Path(recovered.audit_path).read_text(encoding="utf-8")
            )
            self.assertEqual(audit[-1]["errors"], ["state.unreadable"])
            self.assertNotIn("broken-json", json.dumps(audit[-1]))

    def test_isolated_hash_tampering_blocks_recovery_without_trusted_snapshot(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "autonomous_agent"
            package_root.mkdir()
            manager = self.default_manager(package_root, root)
            manager.state = {
                "schema_version": 1,
                "status": "completed",
            }
            manager.save()
            history_path = Path(manager.history_path)
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history[0]["hash"] = "tampered"
            history_path.write_text(json.dumps(history), encoding="utf-8")
            Path(manager.path).write_text(
                json.dumps({"schema_version": 999, "status": "completed"}),
                encoding="utf-8",
            )

            recovered = self.default_manager(package_root, root)
            result = recovered.validate_or_recover()

            self.assertFalse(result["accepted"])
            self.assertFalse(result["recovered"])
            self.assertEqual(recovered.state["schema_version"], 999)


if __name__ == "__main__":
    unittest.main()
