import json
import tempfile
import unittest
from pathlib import Path

from autonomous_agent.recovery_authority import RecoveryAuthority
from autonomous_agent.recovery_gate import RecoveryGate
from autonomous_agent.recovery_governance import RecoveryGovernanceEngine
from autonomous_agent.recovery_schema import StateSchemaValidator
from autonomous_agent.state_manager import StateManager


class StateSchemaValidatorTests(unittest.TestCase):

    def setUp(self):
        self.validator = StateSchemaValidator()

    def test_accepts_valid_state(self):
        result = self.validator.validate({
            "schema_version": 1,
            "status": "completed",
            "recovery_event": False,
            "recovery_trust": 1.0,
        })

        self.assertTrue(result["valid"])
        self.assertEqual(result["errors"], [])

    def test_rejects_unsupported_schema_version(self):
        result = self.validator.validate({
            "schema_version": 999,
            "status": "completed",
        })

        self.assertFalse(result["valid"])
        self.assertIn("schema_version.unsupported", result["errors"])

    def test_rejects_unknown_state_field(self):
        result = self.validator.validate({
            "schema_version": 1,
            "corrupted_test": True,
        })

        self.assertFalse(result["valid"])
        self.assertIn("field.unknown:corrupted_test", result["errors"])

    def test_rejects_corrupt_nested_structure(self):
        result = self.validator.validate({
            "schema_version": 1,
            "recovery_audit_chain": ["not-a-record"],
        })

        self.assertFalse(result["valid"])
        self.assertIn(
            "field.recovery_audit_chain[0].expected_object",
            result["errors"],
        )


class StateSchemaRecoveryTests(unittest.TestCase):

    def make_manager(self, directory):
        return StateManager(str(Path(directory) / "agent_state.json"))

    def write_state(self, directory, state):
        path = Path(directory) / "agent_state.json"
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_missing_state_is_accepted_for_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)

            result = manager.validate_or_recover()

            self.assertTrue(result["accepted"])
            self.assertFalse(result["recovered"])

    def test_valid_state_allows_auto_recovery_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
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
            gate = RecoveryGate(governance)
            recovery_allowed = gate.authorize_recovery(decision)
            authority = RecoveryAuthority(
                manager,
                decision,
                recovery_allowed,
            )

            result = authority.evaluate()

            self.assertTrue(recovery_allowed)
            self.assertEqual(result["authority"], "AUTO_RECOVERY")
            self.assertFalse(result["execution_blocked"])

    def test_wrong_version_recovers_last_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager.state = {
                "schema_version": 1,
                "status": "completed",
                "recovery_event": False,
            }
            manager.save()

            self.write_state(directory, {
                "schema_version": 999,
                "status": "completed",
            })

            recovered = self.make_manager(directory)
            result = recovered.validate_or_recover()

            self.assertTrue(result["accepted"])
            self.assertTrue(result["recovered"])
            self.assertEqual(recovered.state["schema_version"], 1)
            self.assertTrue(recovered.state["recovery_event"])
            self.assertEqual(
                recovered.state["recovery_reason"],
                "schema_validation_failure",
            )

            audit = json.loads(
                Path(recovered.audit_path).read_text(encoding="utf-8")
            )
            self.assertEqual(audit[-2]["event"], "state_schema_violation")
            self.assertEqual(audit[-1]["event"], "state_recovery")

    def test_schema_invalid_snapshot_breaks_trusted_recovery_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager.state = {
                "schema_version": 1,
                "status": "completed",
            }
            valid_snapshot = manager.save_snapshot("known-good")

            manager.state["corrupted_test"] = True
            manager.save_snapshot("schema-invalid")

            manager.state.pop("corrupted_test")
            manager.state["status"] = "valid-looking-descendant"
            manager.save_snapshot("after-schema-invalid")

            manager.state["corrupted_test"] = True
            manager.save(snapshot=False)

            recovered = self.make_manager(directory)
            result = recovered.validate_or_recover()

            self.assertTrue(result["accepted"])
            self.assertEqual(
                result["snapshot"]["id"],
                valid_snapshot["id"],
            )
            self.assertNotIn("corrupted_test", recovered.state)
            self.assertEqual(recovered.state["status"], "completed")

    def test_hash_tampering_blocks_recovery_without_safe_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(directory)
            manager.state = {
                "schema_version": 1,
                "status": "completed",
            }
            manager.save()

            history_path = Path(manager.history_path)
            history = json.loads(history_path.read_text(encoding="utf-8"))
            history[0]["hash"] = "tampered"
            history_path.write_text(json.dumps(history), encoding="utf-8")

            self.write_state(directory, {
                "schema_version": 999,
                "status": "completed",
            })

            recovered = self.make_manager(directory)
            result = recovered.validate_or_recover()

            self.assertFalse(result["accepted"])
            self.assertFalse(result["recovered"])
            self.assertEqual(recovered.state["schema_version"], 999)


if __name__ == "__main__":
    unittest.main()
