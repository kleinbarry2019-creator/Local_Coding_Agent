# SAFE AGENT V50 Writer Migration Status

## Runtime State

| Writer | Current Location | Target | Status |
|---|---|---|---|
| agent_state | V49 path | state/ | migrated for V50 |
| modular recovery_state | repository-root path | state/recovery_state.json | migrated for V50 |

## Audit

| Writer | Current Location | Target | Status |
|---|---|---|---|
| audit_log | V49 path | state/audit/ | migrated for V50 |
| audit_head | V49 path | state/audit/ | migrated for V50 |
| recovery_audit | repository-root path | state/audit/recovery_audit.json | migrated for V50 |

## Snapshots

| Writer | Current Location | Target | Status |
|---|---|---|---|
| recovery_history | repository-root path | state/snapshots/recovery_history.json | migrated for V50 |

## Integrity

| Writer | Current Location | Target | Status |
|---|---|---|---|
| sha256 files | V49 path | state/ | migrated for V50 |

## Migration Rule

No writer is changed until:

- location identified
- compatibility checked
- test added
- release validation passes

## Compatibility

`safe_agent_v50.py` uses the centralized `runtime_paths` abstraction. Existing
V50 runtime files are copied into the isolated layout only when a destination
does not already exist. V49 sources and legacy runtime files are not modified.
Default modular `StateManager` files migrate independently from the standalone
V50 schema. Explicit-path callers retain their existing paths and bypass the
default migration.
