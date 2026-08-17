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

- [ ] Locate current state writers
- [ ] Locate audit writers
- [ ] Locate snapshot writers
- [ ] Add V50 path abstraction
- [ ] Redirect writers
- [ ] Validate V49 compatibility

