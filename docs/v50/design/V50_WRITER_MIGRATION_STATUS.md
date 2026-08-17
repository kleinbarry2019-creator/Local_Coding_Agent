# SAFE AGENT V50 Writer Migration Status

## Runtime State

| Writer | Current Location | Target | Status |
|---|---|---|---|
| agent_state | V49 path | state/ | pending |

## Audit

| Writer | Current Location | Target | Status |
|---|---|---|---|
| audit_log | V49 path | state/audit/ | pending |
| audit_head | V49 path | state/audit/ | pending |

## Integrity

| Writer | Current Location | Target | Status |
|---|---|---|---|
| sha256 files | V49 path | state/ | pending |

## Migration Rule

No writer is changed until:

- location identified
- compatibility checked
- test added
- release validation passes
