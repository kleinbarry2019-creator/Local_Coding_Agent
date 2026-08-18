# Modular Recovery Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the modular `StateManager` state, snapshot history, and recovery audit data into isolated V50 runtime storage while preserving legacy files and explicit-path compatibility.

**Architecture:** Extend `RuntimePaths` with three recovery-specific destinations and add a fail-closed, atomic, non-destructive migration function for the matching legacy root files. `StateManager()` selects and migrates those defaults only when `path` is omitted; callers that provide `path` retain the existing companion-file derivation and never trigger default migration.

**Tech Stack:** Python 3 standard library (`pathlib`, `tempfile`, `shutil`, `os`, `unittest`, `unittest.mock`), GitHub Actions YAML, Flake8, Bandit.

**Spec:** `docs/superpowers/specs/2026-08-18-modular-recovery-storage-design.md`

## Global Constraints

- Keep standalone V50 state at `autonomous_agent/state/agent_state.json`; modular recovery state must use `autonomous_agent/state/recovery_state.json`.
- Store modular history at `autonomous_agent/state/snapshots/recovery_history.json` and modular audit events at `autonomous_agent/state/audit/recovery_audit.json`.
- Preserve V49 source files and implementations unchanged.
- Preserve legacy `agent_state.json`, `agent_history.json`, and `agent_audit.json` as rollback artifacts; never delete or overwrite them.
- Never overwrite an existing isolated destination and never merge divergent state, history, or audit files.
- Reject symlink sources, symlink destinations, and non-regular source or destination paths before copying.
- Publish each migrated copy atomically through a temporary file in the destination directory.
- Keep explicit `StateManager(path, history_path, audit_path)` behavior and companion-path derivation unchanged.
- Preserve recovery policy thresholds, governance behavior, schema version `1`, and hash-chain validation behavior.
- Audit errors may contain error codes and field names, but not state values.
- Rollout is one focused implementation commit after `Design modular recovery storage migration`.
- Full validation must leave the tracked worktree clean.

## File Structure

- Modify `autonomous_agent/runtime_paths.py`: own recovery path definitions and non-destructive legacy recovery migration.
- Modify `autonomous_agent/tests/test_runtime_paths.py`: prove path separation and all migration safety invariants.
- Create `autonomous_agent/tests/test_recovery_storage.py`: prove default `StateManager` integration, explicit-path compatibility, and isolated recovery behavior.
- Modify `autonomous_agent/state_manager.py`: select centralized defaults only for an omitted `path`, invoke default migration, and retain custom path semantics.
- Modify `autonomous_agent/state/README.md`: document standalone and modular runtime files and rollback behavior.
- Modify `docs/v50/design/V50_WRITER_MIGRATION_STATUS.md`: record the modular state, history, and audit writers as migrated.
- Modify `docs/v50/design/RUNTIME_MIGRATION_MATRIX.md`: mark snapshot-writer migration complete.
- Modify `.github/workflows/release-gate.yml`: compile `state_manager.py` and run recovery storage/schema tests in the release gate.
- Keep `docs/superpowers/plans/2026-08-18-modular-recovery-storage.md` as the execution record tracked by the design commit.

---

### Task 1: Recovery Path Definitions and Safe Migration

**Files:**
- Modify: `autonomous_agent/runtime_paths.py:8-132`
- Modify: `autonomous_agent/tests/test_runtime_paths.py:1-112`

**Interfaces:**
- Consumes: `RuntimePaths`, `build_runtime_paths(package_root)`, `ensure_runtime_directories(paths)`.
- Produces: `RuntimePaths.recovery_state_file: Path`, `RuntimePaths.recovery_history_file: Path`, `RuntimePaths.recovery_audit_file: Path`, and `migrate_legacy_recovery_files(paths, legacy_root=None) -> tuple[Path, ...]`.

- [ ] **Step 1: Add failing isolated-path and collision tests**

Extend `test_builds_isolated_v50_layout` in `autonomous_agent/tests/test_runtime_paths.py` with these exact assertions:

```python
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
```

- [ ] **Step 2: Run the path test and verify the intended failure**

Run:

```bash
python -m unittest autonomous_agent.tests.test_runtime_paths.RuntimePathsTests.test_builds_isolated_v50_layout -v
```

Expected: `ERROR` with `AttributeError: 'RuntimePaths' object has no attribute 'recovery_state_file'`.

- [ ] **Step 3: Add the minimal recovery path fields**

Add these fields to `RuntimePaths` after `audit_head`:

```python
recovery_state_file: Path
recovery_history_file: Path
recovery_audit_file: Path
```

Add these keyword arguments to the `RuntimePaths(...)` value returned by `build_runtime_paths`:

```python
recovery_state_file=state_root / "recovery_state.json",
recovery_history_file=state_root / "snapshots/recovery_history.json",
recovery_audit_file=audit_root / "recovery_audit.json",
```

- [ ] **Step 4: Run the isolated-path test and verify it passes**

Run:

```bash
python -m unittest autonomous_agent.tests.test_runtime_paths.RuntimePathsTests.test_builds_isolated_v50_layout -v
```

Expected: `OK`.

- [ ] **Step 5: Add failing non-destructive migration tests**

Add `import stat` and import `migrate_legacy_recovery_files` from `autonomous_agent.runtime_paths`. First extend `test_creates_runtime_directories` with owner-only mode checks:

```python
for path in (
    paths.runtime_root,
    paths.state_root,
    paths.audit_root,
    paths.snapshot_root,
    paths.cache_root,
):
    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
```

Then add these methods to `RuntimePathsTests`:

```python
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
            self.assertEqual((root / name).read_text(encoding="utf-8"), value)

def test_recovery_migration_does_not_overwrite_existing_destination(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = build_runtime_paths(root)
        ensure_runtime_directories(paths)
        (root / "agent_state.json").write_text("legacy", encoding="utf-8")
        paths.recovery_state_file.write_text("isolated", encoding="utf-8")

        migrated = migrate_legacy_recovery_files(paths, legacy_root=root)

        self.assertNotIn(paths.recovery_state_file, migrated)
        self.assertEqual(
            paths.recovery_state_file.read_text(encoding="utf-8"),
            "isolated",
        )
```

- [ ] **Step 6: Run both migration tests and verify the intended failure**

Run:

```bash
python -m unittest \
  autonomous_agent.tests.test_runtime_paths.RuntimePathsTests.test_migrates_legacy_recovery_files_without_deleting_sources \
  autonomous_agent.tests.test_runtime_paths.RuntimePathsTests.test_recovery_migration_does_not_overwrite_existing_destination \
  -v
```

Expected: import failure because `migrate_legacy_recovery_files` does not exist.

- [ ] **Step 7: Refactor atomic copy logic and implement recovery migration**

Replace the duplicated loop body in `migrate_legacy_runtime_files` with a private helper and add the recovery-specific entry point:

```python
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
            shutil.copy2(source, temporary, follow_symlinks=False)
            try:
                os.link(temporary, destination, follow_symlinks=False)
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
```

Keep `migrate_legacy_runtime_files` responsible for its existing five standalone mappings, but change its final migration loop to:

```python
return _migrate_files(migrations)
```

- [ ] **Step 8: Run all runtime path tests**

Run:

```bash
python -m unittest autonomous_agent.tests.test_runtime_paths -v
```

Expected: all tests pass.

- [ ] **Step 9: Add failing recovery migration path-safety tests**

Add these methods to `RuntimePathsTests`:

```python
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
```

- [ ] **Step 10: Run the path-safety tests and verify they pass**

Run:

```bash
python -m unittest autonomous_agent.tests.test_runtime_paths -v
```

Expected: all runtime path tests pass, including source symlink, destination symlink, source type, and destination type rejection.

- [ ] **Step 11: Add and run a copy-failure rollback test**

Add `from unittest.mock import patch` to the test imports, then add this method to `RuntimePathsTests`:

```python
def test_recovery_copy_failure_preserves_source_and_cleans_temporary_file(self):
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

        self.assertEqual(source.read_text(encoding="utf-8"), "rollback-state")
        self.assertFalse(paths.recovery_state_file.exists())
        self.assertEqual(
            list(paths.state_root.glob(".recovery_state.json.migration.*")),
            [],
        )
```

Run:

```bash
python -m unittest \
  autonomous_agent.tests.test_runtime_paths.RuntimePathsTests.test_recovery_copy_failure_preserves_source_and_cleans_temporary_file \
  -v
```

Expected: `OK`, proving a copy error leaves the rollback source unchanged and removes the unpublished temporary file.

- [ ] **Step 12: Review the Task 1 diff without committing**

Run:

```bash
git diff -- autonomous_agent/runtime_paths.py autonomous_agent/tests/test_runtime_paths.py
git diff --check
```

Expected: only recovery path/migration changes are present and `git diff --check` exits `0`. Keep the changes uncommitted because the approved design requires one focused implementation commit.

---

### Task 2: StateManager Default Storage Integration

**Files:**
- Create: `autonomous_agent/tests/test_recovery_storage.py`
- Modify: `autonomous_agent/state_manager.py:1-30`

**Interfaces:**
- Consumes: `build_runtime_paths(package_root) -> RuntimePaths` and `migrate_legacy_recovery_files(paths, legacy_root) -> tuple[Path, ...]` from Task 1.
- Produces: `StateManager(path=None, history_path=None, audit_path=None)` where an omitted `path` activates isolated defaults and any supplied `path` retains legacy custom-path behavior.

- [ ] **Step 1: Add a failing default-path integration test**

Create `autonomous_agent/tests/test_recovery_storage.py` with imports, a reusable module patch, and the first test:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomous_agent import state_manager as state_manager_module
from autonomous_agent.state_manager import StateManager


class RecoveryStorageTests(unittest.TestCase):

    def default_manager(self, package_root, legacy_root):
        with (
            patch.object(state_manager_module, "PACKAGE_ROOT", package_root),
            patch.object(state_manager_module.Path, "cwd", return_value=legacy_root),
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the default-path test and verify the intended failure**

Run:

```bash
python -m unittest autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_default_manager_uses_isolated_recovery_paths -v
```

Expected: failure because `StateManager()` still resolves `agent_state.json`, `agent_history.json`, and `agent_audit.json` relative to the current directory.

- [ ] **Step 3: Implement omitted-path resolution and default migration**

Replace the `os` import and add runtime path imports at the top of `autonomous_agent/state_manager.py`:

```python
from pathlib import Path

from autonomous_agent.recovery_schema import StateSchemaValidator
from autonomous_agent.runtime_paths import (
    build_runtime_paths,
    migrate_legacy_recovery_files,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
```

Change the constructor signature and path selection to:

```python
def __init__(self, path=None, history_path=None, audit_path=None):
    if path is None:
        runtime_paths = build_runtime_paths(PACKAGE_ROOT)
        migrate_legacy_recovery_files(
            runtime_paths,
            legacy_root=Path.cwd(),
        )
        path = runtime_paths.recovery_state_file
        history_path = history_path or runtime_paths.recovery_history_file
        audit_path = audit_path or runtime_paths.recovery_audit_file
    else:
        path = Path(path)
        state_directory = path.parent
        history_path = history_path or state_directory / "agent_history.json"
        audit_path = audit_path or state_directory / "agent_audit.json"

    self.path = str(path)
    self.state = {}
    self.history_path = str(history_path)
    self.audit_path = str(audit_path)
```

Retain the remainder of constructor initialization and `self.load()` unchanged. Retaining string attributes avoids changing existing callers that pass paths to `os.path` and built-in file APIs.

- [ ] **Step 4: Run the default-path integration test**

Run:

```bash
python -m unittest autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_default_manager_uses_isolated_recovery_paths -v
```

Expected: `OK`.

- [ ] **Step 5: Add failing migration and authoritative-destination integration tests**

Add these methods to `RecoveryStorageTests`:

```python
def test_default_manager_migrates_all_legacy_files_and_keeps_sources(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_root = root / "autonomous_agent"
        package_root.mkdir()
        legacy = {
            "agent_state.json": {"schema_version": 1, "status": "legacy"},
            "agent_history.json": [],
            "agent_audit.json": [{"event": "legacy"}],
        }
        for name, value in legacy.items():
            (root / name).write_text(json.dumps(value), encoding="utf-8")

        manager = self.default_manager(package_root, root)

        self.assertEqual(manager.state["status"], "legacy")
        self.assertEqual(
            json.loads(Path(manager.history_path).read_text(encoding="utf-8")),
            [],
        )
        self.assertEqual(
            json.loads(Path(manager.audit_path).read_text(encoding="utf-8")),
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
```

- [ ] **Step 6: Run the default migration integration tests**

Run:

```bash
python -m unittest \
  autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_default_manager_migrates_all_legacy_files_and_keeps_sources \
  autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_existing_isolated_state_is_authoritative \
  -v
```

Expected: both tests pass.

- [ ] **Step 7: Add explicit-path compatibility tests**

Add these methods to `RecoveryStorageTests`:

```python
def test_explicit_path_derives_companions_and_bypasses_default_migration(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        custom_root = root / "embedded"
        custom_root.mkdir()
        custom_state = custom_root / "custom_state.json"
        (root / "agent_state.json").write_text(
            json.dumps({"schema_version": 1, "status": "legacy"}),
            encoding="utf-8",
        )

        with patch(
            "autonomous_agent.state_manager.migrate_legacy_recovery_files"
        ) as migrate:
            manager = StateManager(custom_state)

        migrate.assert_not_called()
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
```

- [ ] **Step 8: Run explicit-path tests and existing schema tests**

Run:

```bash
python -m unittest \
  autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_explicit_path_derives_companions_and_bypasses_default_migration \
  autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_explicit_companion_paths_are_preserved \
  autonomous_agent.tests.test_recovery_schema \
  -v
```

Expected: all tests pass, proving existing explicit-path recovery cases remain compatible.

- [ ] **Step 9: Review the Task 2 diff without committing**

Run:

```bash
git diff -- autonomous_agent/state_manager.py autonomous_agent/tests/test_recovery_storage.py
git diff --check
```

Expected: default instances use centralized storage, explicit instances bypass migration, and `git diff --check` exits `0`. Keep the changes uncommitted for the single implementation commit.

---

### Task 3: Isolated Schema and Hash-Chain Recovery Regression

**Files:**
- Modify: `autonomous_agent/tests/test_recovery_storage.py`

**Interfaces:**
- Consumes: default `StateManager()` storage selection from Task 2 and existing `save`, `save_snapshot`, `validate_or_recover`, and governance APIs.
- Produces: regression coverage proving automatic authorization, last-trusted-snapshot recovery, audit isolation, and fail-closed hash handling on centralized paths.

- [ ] **Step 1: Add a valid-state authorization test on isolated storage**

Add these imports to `autonomous_agent/tests/test_recovery_storage.py`:

```python
from autonomous_agent.recovery_authority import RecoveryAuthority
from autonomous_agent.recovery_gate import RecoveryGate
from autonomous_agent.recovery_governance import RecoveryGovernanceEngine
```

Add this test:

```python
def test_valid_isolated_state_retains_auto_recovery_authority(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_root = root / "autonomous_agent"
        package_root.mkdir()
        manager = self.default_manager(package_root, root)
        manager.state = {"schema_version": 1, "status": "completed"}
        decision = {"action": "continue", "reason": "state_healthy"}
        governance = RecoveryGovernanceEngine(manager)
        authorization = governance.authorize(decision)
        manager.state["recovery_confidence"] = authorization["confidence"]
        decision["confidence"] = authorization["confidence"]
        allowed = RecoveryGate(governance).authorize_recovery(decision)

        result = RecoveryAuthority(manager, decision, allowed).evaluate()

        self.assertTrue(allowed)
        self.assertEqual(result["authority"], "AUTO_RECOVERY")
        self.assertFalse(result["execution_blocked"])
```

- [ ] **Step 2: Run the authorization test**

Run:

```bash
python -m unittest autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_valid_isolated_state_retains_auto_recovery_authority -v
```

Expected: `OK`; the default path change does not alter governance thresholds or authorization behavior.

- [ ] **Step 3: Add last-trusted-snapshot recovery on isolated storage**

Add this test:

```python
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
        audit = json.loads(Path(recovered.audit_path).read_text(encoding="utf-8"))
        self.assertEqual(audit[-2]["event"], "state_schema_violation")
        self.assertEqual(audit[-2]["errors"], ["schema_version.unsupported"])
        self.assertNotIn("completed", json.dumps(audit[-2]))
        self.assertEqual(audit[-1]["event"], "state_recovery")
        self.assertFalse((root / "agent_audit.json").exists())
```

- [ ] **Step 4: Run the isolated snapshot recovery test**

Run:

```bash
python -m unittest autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_invalid_isolated_schema_restores_last_trusted_snapshot -v
```

Expected: `OK`; the restored state comes from `state/snapshots/recovery_history.json`, audit records go only to `state/audit/recovery_audit.json`, and schema field values are not copied into the violation event.

- [ ] **Step 5: Add malformed-state fail-closed coverage**

Add this test:

```python
def test_malformed_isolated_state_is_not_treated_as_healthy_empty_state(self):
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
        audit = json.loads(Path(recovered.audit_path).read_text(encoding="utf-8"))
        self.assertEqual(audit[-1]["errors"], ["state.unreadable"])
        self.assertNotIn("broken-json", json.dumps(audit[-1]))
```

- [ ] **Step 6: Run the malformed-state test**

Run:

```bash
python -m unittest autonomous_agent.tests.test_recovery_storage.RecoveryStorageTests.test_malformed_isolated_state_is_not_treated_as_healthy_empty_state -v
```

Expected: `OK`; unreadable JSON is rejected, no nonexistent snapshot is restored, and the audit event contains only the error code.

- [ ] **Step 7: Add fail-closed hash tampering coverage**

Add this test:

```python
def test_isolated_hash_tampering_blocks_recovery_without_trusted_snapshot(self):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package_root = root / "autonomous_agent"
        package_root.mkdir()
        manager = self.default_manager(package_root, root)
        manager.state = {"schema_version": 1, "status": "completed"}
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
```

- [ ] **Step 8: Run all recovery storage and recovery schema tests**

Run:

```bash
python -m unittest \
  autonomous_agent.tests.test_recovery_storage \
  autonomous_agent.tests.test_recovery_schema \
  -v
```

Expected: all tests pass. No policy, governance, schema, or hash algorithm changes are necessary.

- [ ] **Step 9: Review the Task 3 diff without committing**

Run:

```bash
git diff -- autonomous_agent/tests/test_recovery_storage.py autonomous_agent/state_manager.py
git diff --check
```

Expected: isolated regression cases cover valid state, invalid schema, schema-invalid descendants, and hash tampering; `git diff --check` exits `0`.

---

### Task 4: Documentation and Release Gate Integration

**Files:**
- Modify: `autonomous_agent/state/README.md:1-18`
- Modify: `docs/v50/design/V50_WRITER_MIGRATION_STATUS.md:1-30`
- Modify: `docs/v50/design/RUNTIME_MIGRATION_MATRIX.md:27-38`
- Modify: `.github/workflows/release-gate.yml:24-37`

**Interfaces:**
- Consumes: finalized runtime filenames and tests from Tasks 1-3.
- Produces: operator-visible layout/rollback documentation and CI execution of `test_runtime_paths`, `test_recovery_storage`, and `test_recovery_schema`.

- [ ] **Step 1: Update the state layout and rollback documentation**

Replace the tree in `autonomous_agent/state/README.md` with:

```text
state/
├── agent_state.json
├── agent_state.json.bak
├── agent_state.sha256
├── recovery_state.json
├── audit/
│   ├── audit_log.jsonl
│   ├── audit_head.sha256
│   └── recovery_audit.json
└── snapshots/
    └── recovery_history.json
```

Replace its migration paragraph with:

```markdown
On first use, `safe_agent_v50.py` copies its legacy runtime files from the
`autonomous_agent/` directory when an isolated destination does not exist.
Default modular `StateManager()` instances separately copy `agent_state.json`,
`agent_history.json`, and `agent_audit.json` from the working directory into
the recovery-specific destinations. Existing isolated destinations are
authoritative. Legacy files are retained unchanged for rollback compatibility;
divergent copies are never merged automatically.
```

- [ ] **Step 2: Mark all recovery writers as migrated**

In `docs/v50/design/V50_WRITER_MIGRATION_STATUS.md`, add these rows to the appropriate tables:

```markdown
| modular recovery_state | repository-root path | state/recovery_state.json | migrated for V50 |
| recovery_audit | repository-root path | state/audit/recovery_audit.json | migrated for V50 |
| recovery_history | repository-root path | state/snapshots/recovery_history.json | migrated for V50 |
```

Extend the compatibility paragraph with:

```markdown
Default modular `StateManager` files migrate independently from the standalone
V50 schema. Explicit-path callers retain their existing paths and bypass the
default migration.
```

In `docs/v50/design/RUNTIME_MIGRATION_MATRIX.md`, add this completed item:

```markdown
- [x] Redirect modular recovery state, snapshot, and audit writers
```

Replace the final pending sentence with:

```markdown
Snapshot-writer migration is complete for the modular recovery subsystem.
```

- [ ] **Step 3: Extend the release gate**

Change the compile step in `.github/workflows/release-gate.yml` to:

```yaml
      - name: Python compile check V50
        run: |
          python -m compileall \
            autonomous_agent/safe_agent_v50.py \
            autonomous_agent/runtime_paths.py \
            autonomous_agent/state_manager.py
```

Change the runtime test step to:

```yaml
      - name: Runtime and recovery storage tests
        run: |
          python -m unittest \
            autonomous_agent.tests.test_runtime_paths \
            autonomous_agent.tests.test_recovery_storage \
            autonomous_agent.tests.test_recovery_schema
```

- [ ] **Step 4: Verify documentation and workflow diffs**

Run:

```bash
git diff -- \
  autonomous_agent/state/README.md \
  docs/v50/design/V50_WRITER_MIGRATION_STATUS.md \
  docs/v50/design/RUNTIME_MIGRATION_MATRIX.md \
  .github/workflows/release-gate.yml
git diff --check
```

Expected: the documented paths exactly match `RuntimePaths`, the matrix no longer calls snapshot migration pending, the release gate runs all three modules, and `git diff --check` exits `0`.

---

### Task 5: Full Validation and Focused Implementation Commit

**Files:**
- Verify: all files changed in Tasks 1-4
- Commit: the complete implementation as one commit after `Design modular recovery storage migration`

**Interfaces:**
- Consumes: all production, test, documentation, and workflow changes from Tasks 1-4.
- Produces: one verified commit whose tests leave tracked files unchanged.

- [ ] **Step 1: Capture the tracked baseline before validation**

Run:

```bash
git status --short
git diff --name-only
```

Expected: only the Task 1-4 implementation files are modified or newly created; no runtime-generated file is tracked.

- [ ] **Step 2: Run focused unit tests**

Run:

```bash
python -m unittest \
  autonomous_agent.tests.test_runtime_paths \
  autonomous_agent.tests.test_recovery_storage \
  autonomous_agent.tests.test_recovery_schema \
  -v
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the full existing V50 regression suite**

Run:

```bash
autonomous_agent/tools/test_all_v50.sh
```

Expected: output ends with `V50 TESTS PASSED`.

- [ ] **Step 4: Compile all changed Python and generated V50 code**

Run:

```bash
python -m compileall \
  autonomous_agent/runtime_paths.py \
  autonomous_agent/state_manager.py \
  autonomous_agent/tests/test_runtime_paths.py \
  autonomous_agent/tests/test_recovery_storage.py \
  autonomous_agent/tests/test_recovery_schema.py \
  autonomous_agent/safe_agent_v50.py
bash -n autonomous_agent/build_agent_v50.sh
```

Expected: both commands exit `0`.

- [ ] **Step 5: Run style and security checks**

Run:

```bash
flake8 --ignore=E303,W503,W504 \
  autonomous_agent/runtime_paths.py \
  autonomous_agent/state_manager.py \
  autonomous_agent/tests/test_runtime_paths.py \
  autonomous_agent/tests/test_recovery_storage.py \
  autonomous_agent/tests/test_recovery_schema.py
bandit -r \
  autonomous_agent/runtime_paths.py \
  autonomous_agent/state_manager.py \
  autonomous_agent/safe_agent_v50.py
```

Expected: both commands exit `0` with no reportable issues. `E303`, `W503`, and `W504` are ignored because `state_manager.py` already contains those pre-existing whitespace styles; all other Flake8 rules remain active. Run the commands separately so a later command cannot mask an earlier nonzero exit.

- [ ] **Step 6: Verify generator compilation**

Run the generator against a disposable copy of the package so tracked source files cannot be replaced and the generated V50 regression tests receive their normal package fixture:

```bash
generator_root="$(mktemp -d)"
cp -a autonomous_agent "$generator_root/"
(
  set -e
  cd "$generator_root/autonomous_agent"
  bash build_agent_v50.sh > build.log 2>&1
  if rg -n 'Traceback|ModuleNotFoundError|AssertionError|FAILED' build.log; then
    exit 1
  fi
  rg -n 'SAFE AGENT V50 READY' build.log
  python -m py_compile safe_agent_v50.py runtime_paths.py tests/test_v50.py
)
```

Expected: the build log has no failure markers, contains `SAFE AGENT V50 READY`, and all three compile checks exit `0`. The temporary directory is outside the repository and may be left for the operating system's temporary-file cleanup.

- [ ] **Step 7: Confirm validation did not dirty tracked files**

Run:

```bash
git status --short
git diff --check
```

Expected: status matches the Task 1-4 implementation baseline from Step 1, with no additional tracked changes, and `git diff --check` exits `0`.

- [ ] **Step 8: Inspect the complete implementation diff**

Run:

```bash
git diff --stat
git diff -- \
  autonomous_agent/runtime_paths.py \
  autonomous_agent/state_manager.py \
  autonomous_agent/tests/test_runtime_paths.py \
  autonomous_agent/tests/test_recovery_storage.py \
  autonomous_agent/state/README.md \
  docs/v50/design/V50_WRITER_MIGRATION_STATUS.md \
  docs/v50/design/RUNTIME_MIGRATION_MATRIX.md \
  .github/workflows/release-gate.yml
```

Expected: every diff hunk maps to an approved requirement and no unrelated file is changed.

- [ ] **Step 9: Create the single focused implementation commit**

Run:

```bash
git add \
  docs/superpowers/plans/2026-08-18-modular-recovery-storage.md \
  autonomous_agent/runtime_paths.py \
  autonomous_agent/state_manager.py \
  autonomous_agent/tests/test_runtime_paths.py \
  autonomous_agent/tests/test_recovery_storage.py \
  autonomous_agent/state/README.md \
  docs/v50/design/V50_WRITER_MIGRATION_STATUS.md \
  docs/v50/design/RUNTIME_MIGRATION_MATRIX.md \
  .github/workflows/release-gate.yml
git commit -m "Migrate modular recovery storage"
```

Expected: one new implementation commit is created directly after the design commit.

- [ ] **Step 10: Verify final history and clean worktree**

Run:

```bash
git log -3 --oneline
git status --short --branch
```

Expected: `Migrate modular recovery storage` is immediately above `Design modular recovery storage migration`, and the isolated worktree is clean. Integration and push follow through the required finishing-development-branch workflow.
