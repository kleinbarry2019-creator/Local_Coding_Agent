# SAFE AGENT V50 Runtime Isolation

## Goal

Separate runtime-generated data from source-controlled code.

## Rules

- Runtime state must not modify tracked source files.
- Audit data must live in dedicated state storage.
- Temporary cache data must be isolated.
- Release checks must run from a clean source tree.

## Layout

autonomous_agent/

runtime/
- cache/
- temporary execution data

state/
- audit/
- snapshots/

## Compatibility

V49 behavior remains unchanged.

V50 introduces separation only.
