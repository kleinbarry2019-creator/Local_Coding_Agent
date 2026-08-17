# SAFE AGENT V50 Runtime Migration Plan

## Purpose

Move runtime-generated data away from source-controlled locations.

## Migration Rules

- Preserve V49 behavior.
- Move only state ownership.
- Keep policy enforcement unchanged.
- Maintain audit integrity.

## Migration Steps

1. Identify runtime writers.
2. Introduce centralized state paths.
3. Redirect audit output.
4. Redirect snapshots.
5. Validate clean repository state.
6. Run full release validation.

## Rollback

Rollback is possible by restoring the previous runtime paths.

## Acceptance Criteria

- Tests pass.
- No runtime files modify tracked source files.
- Audit chain remains valid.
- Release check succeeds.
