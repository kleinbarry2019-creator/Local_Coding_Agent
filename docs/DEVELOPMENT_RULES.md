# SAFE AGENT Development Rules

## Versioning

Every release must have:

- reproducible git state
- release tag
- validation result
- rollback path

## Change Process

Every change follows:

1. Modify
2. Test
3. Document
4. Commit

## Plugin and GitHub Error Correction

Every release or pull-request failure follows this mandatory sequence:

1. Reproduce the exact failure and capture the command, commit SHA, and check URL.
2. Use systematic debugging before proposing a fix.
3. Inspect GitHub check logs with the GitHub plugin or `gh` fallback.
4. Use the matching installed review/security plugin when callable.
5. Write or tighten a regression test before production changes.
6. Correct the root cause, rerun the focused command, then the full gate.
7. Request independent review and resolve all Critical or Important feedback.
8. Push, wait for GitHub checks, and verify the reviewed SHA matches local `HEAD`.
9. Report unavailable plugin endpoints instead of claiming they ran.

## Safety Requirements

- No uncontrolled filesystem access
- No hidden state mutation
- No bypass of policy checks
- Audit trail must remain verifiable

## Runtime Data

Runtime-generated files must not pollute source history.

State, logs and temporary data must be isolated.

## Release Criteria

A release requires:

- tests passing
- clean git state
- version verification
- documented changes
