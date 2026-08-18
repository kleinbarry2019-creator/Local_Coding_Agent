# SAFE AGENT V50 Writer Migration Status

## Runtime State

| Writer | Current Location | Target | Status |
|---|---|---|---|
| agent_state | V49 path | state/ | migrated for V50 |

## Audit

| Writer | Current Location | Target | Status |
|---|---|---|---|
| audit_log | V49 path | state/audit/ | migrated for V50 |
| audit_head | V49 path | state/audit/ | migrated for V50 |

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
