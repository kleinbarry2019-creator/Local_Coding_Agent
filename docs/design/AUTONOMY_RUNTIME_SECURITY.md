# Autonomy Runtime Security and Recovery Design

## Trust boundaries

- Natural-language text, file paths, process arguments, model output, and tool
  output are untrusted.
- `GoalNormalizer` accepts only bounded deterministic intents and rejects
  ambiguous or traversing targets.
- `ProjectPathResolver` canonicalizes the project root and rejects symlink or
  out-of-scope targets before policy evidence is built.
- `ToolRegistry` validates typed input/output, constructs the exact action
  digest, applies policy, deadlines, and output caps, and is the only active
  tool handler invocation boundary.
- Process execution never uses a shell. Bubblewrap removes the network,
  capabilities, host home, and mutable host filesystem while binding only the
  canonical project as `/workspace`.

## Privileged operations

The main process must be unprivileged. External installation is available only
for a fixed source-controlled mapping of tool, manager, package, and argument
vector. The runtime first builds the exact typed tool request, then issues a
two-minute authority grant for that request digest and capability only. The
executor compares the recipe to the catalog, refuses a root parent process,
uses no shell, requests non-interactive elevation for one child when needed,
and verifies the executable and its version after return.

## Recovery

Before a project file mutation, the runtime captures existence, bytes, size,
and SHA-256 in the owner-only core state. Failed mutations are restored and the
restored bytes are hash-checked. Task status, step index, attempt count, failure
fingerprint, checkpoint transitions, and final completion evidence are written
through the locked audit transaction. A restarted runtime can resume an
incomplete session from durable state. Repeated identical failures stop the
loop rather than consuming an unbounded retry budget.

System package installation is deliberately not described as rollback-safe:
the operation is non-destructive, catalog-bound, and verified, but package
manager rollback semantics vary. A destructive system action has no registered
tool and remains denied without matching recovery evidence.
