# Modular Recovery Storage Design

## Purpose

Move the modular `StateManager` recovery state, snapshot history, and audit
events into the centralized V50 runtime layout without sharing files with the
standalone `safe_agent_v50.py` state format.

The change completes the snapshot-writer migration milestone while preserving
existing state as a rollback source.

## Scope

This milestone covers:

- centralized paths for modular recovery state, history, and audit data;
- non-destructive migration from the current repository-root files;
- fail-closed path and migration checks;
- default `StateManager` integration;
- compatibility for callers that pass explicit paths;
- regression tests and migration documentation.

It does not change recovery policy thresholds, agent execution policy, the
standalone V50 state schema, or legacy V49 implementations.

## Selected Architecture

`runtime_paths.py` remains the sole owner of default mutable paths. It gains
three modular recovery destinations:

```text
autonomous_agent/state/
├── agent_state.json                 # standalone safe_agent_v50 state
├── agent_state.json.bak
├── agent_state.sha256
├── recovery_state.json              # modular StateManager state
├── audit/
│   ├── audit_log.jsonl              # standalone chained audit log
│   ├── audit_head.sha256
│   └── recovery_audit.json          # modular recovery events
└── snapshots/
    └── recovery_history.json        # modular hash-linked snapshots
```

Separate filenames are mandatory because `safe_agent_v50.py` and the modular
`StateManager` use incompatible state schemas.

## Component Responsibilities

### `runtime_paths.py`

- Defines all default modular recovery destinations.
- Creates required runtime directories.
- Copies legacy modular files into isolated storage without deleting sources.
- Rejects source or destination symlinks and non-file destinations.
- Never overwrites an existing isolated destination.

### `StateManager`

- Uses the centralized modular paths only when no explicit paths are supplied.
- Preserves explicit `path`, `history_path`, and `audit_path` behavior for
  tests and embedded callers.
- Runs default-path migration before loading state.
- Continues schema validation before recovery decisions.
- Restores only snapshots whose hash chain and schema are both trusted.

### `main.py`

- Continues constructing `StateManager()` without hard-coded paths.
- Receives isolated defaults through the path abstraction.
- Stops before policy evaluation when neither current state nor a safe snapshot
  is acceptable.

## Migration Rules

Default `StateManager()` construction checks these legacy files relative to the
current working directory:

| Legacy source | Isolated destination |
|---|---|
| `agent_state.json` | `state/recovery_state.json` |
| `agent_history.json` | `state/snapshots/recovery_history.json` |
| `agent_audit.json` | `state/audit/recovery_audit.json` |

Migration follows these rules:

1. Create isolated parent directories with owner-only defaults.
2. Reject symlink sources, symlink destinations, and unexpected file types.
3. Copy through a temporary file in the destination directory.
4. Atomically publish the copied destination.
5. Preserve the legacy source for rollback.
6. Skip migration when the isolated destination already exists.
7. Treat the isolated destination as authoritative after migration.

The migration does not merge divergent files. Silent merging would make audit
and snapshot ordering ambiguous.

## Data Flow

### Normal startup

1. `StateManager()` resolves centralized recovery paths.
2. Legacy files are copied only when isolated destinations are absent.
3. The current recovery state is loaded and migrated to schema version 1 when
   it is a supported legacy shape.
4. The schema validator checks the active state.
5. Recovery policy and governance execute only after acceptance.

### Invalid active state

1. Schema validation records a violation in `recovery_audit.json`.
2. Snapshot history is traversed from genesis while checking hashes and schema.
3. Trust stops at the first invalid hash or invalid schema.
4. The last trusted snapshot is restored to `recovery_state.json`.
5. If no trusted snapshot exists, startup stops fail-closed.

### Explicit-path caller

1. A caller supplies `path` and optionally history or audit paths.
2. `StateManager` derives unspecified companion files beside the supplied state
   path, preserving current test and embedding behavior.
3. No repository-root migration runs for explicit-path instances.

## Error Handling and Security

- Malformed JSON is treated as invalid state, not as an empty healthy state.
- Migration never follows symlinks.
- Existing isolated files are never replaced by legacy copies.
- A non-regular source or destination fails closed.
- Snapshot schema failure breaks trust for that snapshot and all descendants.
- Audit records contain error codes and field names, not state values.
- File-copy failure leaves the legacy source untouched.

## Compatibility

- Existing standalone V50 files keep their current names and behavior.
- V49 source files and implementations remain unchanged.
- Existing modular root files remain available as rollback artifacts.
- Explicit-path `StateManager` users retain their existing directory layout.
- New default writes no longer create mutable files in the repository root.

## Testing Strategy

Implementation follows test-driven development. Tests must first demonstrate
the missing behavior and fail before production changes are added.

Required cases:

1. Default recovery paths resolve to the isolated layout.
2. Standalone and modular state files cannot collide.
3. Three legacy modular files migrate without source deletion.
4. Existing isolated files are not overwritten.
5. Explicit paths bypass default migration.
6. Source and destination symlinks fail closed.
7. Unexpected destination types fail closed.
8. Valid state retains automatic recovery authorization.
9. Invalid schema restores the last trusted isolated snapshot.
10. Hash tampering blocks recovery when no trusted snapshot remains.
11. Full V50 tests leave the tracked worktree clean.

Validation commands include unit tests, V50 regression tests, Python compile,
Flake8, Bandit, generator compilation, and `git diff --check`.

## Documentation and Release Integration

- Update the state-layout README with modular recovery files.
- Mark snapshot-writer migration complete in the V50 migration documents.
- Extend the release gate to run modular recovery storage tests.
- Keep runtime data ignored while preserving tracked documentation.

## Rollout and Rollback

Rollout occurs on the existing `v50-development-v50.6.2` branch as one focused
implementation commit after the design commit.

Rollback requires reverting the implementation commit. Because migration keeps
legacy files intact, the pre-migration modular state remains available. If the
new isolated destination has advanced, operators must choose explicitly which
copy is authoritative; the application will not merge histories automatically.

## Completion Criteria

The milestone is complete when:

- all default modular writes use isolated paths;
- legacy files remain unchanged after migration;
- schema and hash recovery tests pass from isolated storage;
- standalone V50 regression tests pass;
- release checks include the new storage tests;
- the tracked worktree remains clean after test execution.
