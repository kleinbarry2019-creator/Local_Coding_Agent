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
