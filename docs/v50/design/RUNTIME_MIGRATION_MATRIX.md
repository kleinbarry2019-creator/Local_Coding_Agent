# SAFE AGENT V50 Runtime Migration Matrix

## Purpose

Classify all discovered runtime writers before code migration.

## Classification

Each runtime write operation must be assigned:

| Category | Allowed Location |
|---|---|
| Source | repository tracked files |
| Runtime State | autonomous_agent/state/ |
| Audit Data | autonomous_agent/state/audit/ |
| Snapshots | autonomous_agent/state/snapshots/ |
| Temporary Data | autonomous_agent/runtime/ |

## Migration Rules

1. No runtime writer may modify tracked source files.
2. Audit writers must preserve integrity chain.
3. Snapshot writers must use isolated storage.
4. Cache writers must be disposable.
5. Release checks must leave git clean.

## Migration Status

Pending:

- [x] Locate current state writers
- [x] Locate audit writers
- [x] Locate snapshot writers
- [x] Add V50 path abstraction
- [x] Redirect the active V50 state, integrity, and audit writers
- [x] Redirect modular recovery state, snapshot, and audit writers
- [x] Preserve V49 and legacy-file compatibility

Snapshot-writer migration is complete for the modular recovery subsystem.
