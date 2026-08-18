# Core Foundation Design

## Purpose

Establish the first stable, installable foundation for the local coding agent
without extending the V50 monolith. This phase introduces the contracts that
later model, workspace, research, TUI, and autonomous-execution phases will
use while keeping every existing V50 entry point operational.

The foundation is local-first, Linux-first, free to run, and safe by default.
It does not execute coding tasks yet. Its user-visible capability is a reliable
`agent doctor` command backed by the same configuration, policy, event, state,
and tool contracts that production execution will use.

## Program Context

The complete product will be delivered as eight independently verified phases:

1. Core Foundation
2. Workspace and Recovery
3. Local Model Runtime
4. Development Tools
5. Research Layer
6. CLI and TUI
7. Autonomy and Root Hardening
8. V50 Migration and Release

This document specifies only phase 1. Later phases require their own approved
designs and implementation plans.

## Goals

Phase 1 must provide:

- a Python package installable with `uv` and an `agent` console command;
- validated global, project, environment, and command-line configuration;
- explicit monitored, autonomous, and unrestricted-root policy modes;
- a typed tool registry and execution-result contract;
- a versioned SQLite state and event store with tamper-evident audit events;
- a read-only `agent doctor` command with human and JSON output;
- stable interfaces that do not depend on V50 internals;
- deterministic tests and release checks for all new contracts.

## Non-Goals

Phase 1 does not include:

- Ollama inference, planning, code generation, or agent loops;
- file editing, shell execution, Git mutation, web research, or plugins;
- worktree creation, recovery checkpoints, or rollback;
- the interactive TUI;
- automatic package installation or system cleanup;
- unrestricted-root command execution;
- migration or deletion of historical V1-V50 files;
- replacement of the modular recovery storage already in production.

These boundaries keep the first plan small enough to implement and verify as a
single unit.

## Selected Architecture

New code lives in a cohesive `autonomous_agent/core/` package beside the legacy
modules:

```text
autonomous_agent/
├── core/
│   ├── config.py       # loading, precedence, validation, redaction
│   ├── events.py       # event envelope and audit hash chain
│   ├── policy.py       # modes, requests, and decisions
│   ├── state.py        # SQLite schema, migrations, transactions
│   ├── tools.py        # tool specifications, registry, results
│   └── doctor.py       # read-only compatibility probes
├── cli.py              # console command dispatch and output formatting
└── ...                 # unchanged V50 and existing modular files
```

`pyproject.toml` defines the package, the `agent` console script, Python
3.12-3.14 support, and development dependency groups. Runtime code uses the
Python standard library in this phase. Test, lint, and type-check dependencies
remain development-only and are resolved reproducibly through `uv`.

Core modules must not use the legacy package-relative runtime-path abstraction
or import `safe_agent_v50.py`, `main.py`, or other historical entry points. The
legacy resolver remains unchanged for V50. The core gets a separate XDG state
resolver described below, and a later adapter will translate between the new
contracts and legacy behavior.

## Configuration

### Sources and precedence

Configuration is merged in this order, with later sources overriding earlier
ones:

1. safe built-in defaults;
2. global user configuration;
3. project configuration;
4. environment variables;
5. explicit command-line options.

The global file is `$XDG_CONFIG_HOME/local-coding-agent/config.toml`, falling
back to `~/.config/local-coding-agent/config.toml`. Before any project
configuration is read, the loader establishes a canonical project root
from an explicit trusted CLI argument or the nearest enclosing Git worktree;
without either, the canonical current directory is used. The root must be an
existing directory and is resolved before `.local-agent.toml` is loaded from
that exact root. The loader uses `tomllib`; it does not execute configuration
code or interpolate shell text.

Environment variables use the `LOCAL_AGENT_` prefix and an explicit mapping.
Arbitrary environment keys are never converted into configuration fields.

### Initial schema

The phase-1 schema contains:

- schema version;
- execution mode;
- project root;
- state directory;
- default and maximum command timeouts;
- maximum captured output size;
- immutable audit requirement for every future mutating action;
- doctor probe timeout;
- free-only dependency policy;
- CPU, RAM, VRAM, and disk warning thresholds.

Configuration sources have different authority. A later source may override a
field only when that source is authorized for the field:

| Field class | Global file | Project file | Environment | Trusted CLI/session |
|---|---|---|---|---|
| presentation and project hints | set | set | explicit mapping | set |
| soft timeout/resource request | within hard maximum | lower only | lower only | within hard maximum |
| canonical project root | never | never | never | explicit CLI only |
| core state-directory override | set | never | never | explicit CLI only |
| execution mode | never | never | never | monitored/autonomous only |
| unrestricted-root authority | never | never | never | separate authority grant only |
| hard safety maximums | never | never | never | never at runtime |
| free-only invariant | never | never | never | never |

`XDG_STATE_HOME` is a standard platform base, not a local-agent override. When
present it must be an absolute, normalized path with safe ancestors; an invalid
value is rejected rather than resolved relative to the project. There is no
`LOCAL_AGENT_STATE_DIR` environment override. A custom state directory can come
only from the owner-controlled global file or an explicit trusted CLI option.
The global file itself must be a regular, non-symlink file owned by the current
user and not writable by group or other users.

Every effective setting records its source provenance. Unknown fields,
unauthorized overrides, invalid types, unsupported schema versions, unsafe
state paths, and contradictory limits fail closed with field-specific errors.
Project configuration can reduce permissions or limits but cannot widen them.
User-facing errors may show field names and safe values but never secret values.

The safe default mode is `monitored`. `autonomous` requires an explicit trusted
CLI/session action. Configuration alone cannot activate `unrestricted-root`; a
future session-authority mechanism must issue a scoped runtime grant.

## Policy Contract

The policy engine evaluates a `PolicyRequest` and returns a `PolicyDecision`.
It does not execute tools.

A request contains only untrusted action claims:

- session and request identifiers;
- requested capabilities;
- side-effect class;
- requested project and targets;
- whether network or privilege elevation is needed;
- whether an action is destructive;
- configured resource budget.

The engine receives trusted evidence separately in an immutable
`PolicyContext`. Only internal authority and recovery factories can construct
that context. It contains the canonical project root, resolver-produced target
scope evidence, hard resource ceilings, an optional scoped session-authority
grant, and optional recovery evidence. Recovery evidence contains a checkpoint
identifier and digest, protected scope, creation time, freshness limit, and
verification result. Caller- or model-supplied booleans never satisfy an
authority or recovery requirement.

Decisions are `allow`, `confirm`, or `deny` and include a stable reason code,
human explanation, applicable limits, and any unmet prerequisites.

The phase-1 policy matrix establishes these invariants:

- fixed-target, registered doctor probes are allowed in every mode, including a
  bounded Unix-socket or loopback-only Ollama health probe classified as local
  diagnostic I/O rather than general network access;
- all other reads and writes require scope evidence produced by the trusted
  path resolver; autonomous targets outside the canonical project root are
  denied, including absolute, parent, symlink, mount, or bind escapes;
- monitored mode allows registered read-only diagnostic probes but requires
  confirmation for writes, network access, arbitrary process execution, or
  privilege elevation;
- autonomous mode may later allow project-scoped development actions but denies
  privilege elevation and destructive system actions;
- unrestricted-root remains denied unless a valid, unexpired, action-scoped
  session grant is present; phase 1 always denies it because no authority
  provider exists yet;
- destructive system actions require fresh recovery evidence whose digest and
  protected scope match the action even when unrestricted-root authority exists;
- missing or ambiguous metadata produces `deny`, never implicit permission.

Only the read-only doctor tools are executable in phase 1, so tests validate the
future-facing policy matrix without creating a premature system executor.

## Tool Contract

Each `ToolSpec` defines:

- a unique stable name and version;
- a short description;
- concrete dataclass input and output types using a supported, recursively
  validated set of primitives, enums, paths, lists, maps, and nested dataclasses;
- required capabilities;
- side-effect and risk classifications;
- default timeout and maximum captured output;
- whether network, elevation, or recovery is required;
- a callable handler.

The registry rejects duplicate names, unsupported type annotations, unbounded
output, non-positive timeouts, and contradictory capability declarations.
Inputs are converted to the declared dataclass before policy evaluation, and
outputs are validated against the declared output dataclass before they cross
the registry boundary. Serialized results have explicit byte, nesting-depth,
collection-size, diagnostic-length, and artifact-metadata limits. Discovery is
static in phase 1. Dynamic plugins and MCP adapters arrive in later phases.

Handlers receive validated input and an immutable execution context. They
return a `ToolResult` containing status, structured data, safe diagnostics,
duration, truncation state, and optional artifact references. Expected tool
failures are results rather than uncaught exceptions. Programmer errors may
raise internally but are converted at the registry boundary. Stateless doctor
errors produce only redacted in-memory diagnostic events and incident IDs;
persistent auditing begins with later stateful or mutating workflows.

The registry asks the policy engine for a decision before invoking a handler.
`confirm` is returned as a gated result and never invokes the handler. Phase 1
executes only trusted doctor handlers. Each external doctor probe receives an
absolute deadline, uses an argument vector rather than a shell string, caps
captured bytes, and terminates its own process group on timeout. The registry
does not claim that an arbitrary in-process Python callable can be preempted;
general tool-process isolation belongs to the Development Tools phase. The
registry never assembles or evaluates shell commands from model-generated text.

## State and Audit Storage

Phase 1 introduces a separate `agent_core.sqlite3` database below
`$XDG_STATE_HOME/local-coding-agent`, falling back to
`~/.local/state/local-coding-agent`. It never writes inside the source checkout,
virtual environment, or installed package. It does not reuse the incompatible
V50 or modular recovery JSON schemas.

Only the global user configuration and an explicit trusted CLI option may
select another core state directory. The resolver canonicalizes the nearest
existing ancestor, rejects symlink ancestors and non-directory components, and
creates new directories with owner-only permissions. Project configuration
cannot redirect state. The legacy package-relative V50 resolver is unchanged.

The initial database contains:

- `schema_migrations` for ordered, transactional migrations;
- `sessions` for identity, mode, lifecycle state, and timestamps;
- `events` for append-only structured events and audit hashes;
- `config_snapshots` for redacted effective configuration.

SQLite uses WAL mode, foreign keys, bounded busy timeouts, and explicit
transactions. Database creation rejects symlink destinations and unexpected
file types. Database, WAL, shared-memory, and audit-head files use owner-only
permissions where the platform supports them.

All events form one globally ordered chain. Every audit event stores a unique,
monotonically increasing sequence, event ID, UTC timestamp, session ID, event
type, allowlisted JSON payload, previous hash, and current hash. Appends use a
serialized `BEGIN IMMEDIATE` transaction with a unique sequence and previous
hash constraint. The hash is calculated from a canonical representation.

The expected sequence and head hash are also stored in an atomically replaced,
owner-only sidecar outside the database. Before reading or changing the head, a
writer acquires both an in-process mutex and an exclusive `fcntl.flock` on a
dedicated owner-only lock file. The lock file is opened with no-follow and
close-on-exec flags after its path, owner, type, and permissions are validated.
The lock covers sidecar verification, pending preparation, the complete SQLite
transaction, and sidecar finalization. Every reader that compares database and
head state, including verification and startup recovery, acquires the same
exclusive lock for its complete consistency snapshot; it cannot observe an
in-flight pending transition. The lock is released automatically if a process
exits.

While holding that cross-process lock, updates use a two-phase anchor protocol:

1. Atomically write and fsync a sidecar containing the committed head plus a
   pending next sequence/hash.
2. In one SQLite transaction, append that exact event and commit any associated
   state change.
3. Atomically replace and fsync the sidecar with the pending head promoted to
   committed and no pending value.

Startup recovery is deterministic. A pending anchor with no corresponding
database row is cleared back to its committed head. A pending anchor with
exactly one matching next row is finalized. Any extra row, hash mismatch,
sequence gap, missing committed row, or database tail without a matching
pending anchor fails closed. State changes must share the event transaction, so
clearing an uncommitted pending anchor cannot leave an unaudited state change.

Verification starts at genesis and fails on gaps, reordering, competing links,
content mutation, missing sidecar, or tail truncation. Sidecar replacement uses
a same-directory temporary file, file fsync, atomic rename, and parent-directory
fsync.

This is tamper-evident against accidental corruption and database-only changes,
not tamper-proof against an attacker who already controls the user account and
can rewrite both database and sidecar. OS-protected signing keys or an external
attestation boundary are deferred to Root Hardening.

Every event type has an allowlisted persistence schema; unknown fields are
dropped by default. The serializer recursively normalizes secret-field names,
redacts exact sensitive values discovered during configuration loading, and
applies value-pattern safeguards. Raw environments, subprocess output,
exceptions, model text, and arbitrary tool payloads are never persisted
wholesale. Callers remain responsible for minimizing sensitive input, but the
storage boundary independently enforces the persistence allowlist.

FTS5 code search, long-term memory, embeddings, and artifact content storage
are reserved for later phases. The database migration mechanism must allow
those tables to be added without rebuilding phase-1 data.

## Doctor Command

`agent doctor` is the only new end-user workflow in phase 1. It is stateless and
performs read-only, time-bounded probes for:

- operating system, architecture, Python, CPU, RAM, swap, and free disk;
- Git and GitHub CLI availability;
- Ollama availability, service health, and local model inventory;
- NVIDIA driver, GPU, and VRAM when present;
- Podman, Bubblewrap, systemd-run, Node.js, npm, `uv`, and quality tools;
- project root, Git worktree state, and runtime-directory writability;
- SQLite features required by later phases;
- effective execution mode and free-only policy state.

Missing optional components produce warnings, not crashes. Missing required
components produce an unhealthy summary and a stable diagnostic code. Probes
must not install software, start services, download models, modify Git, create
directories, create or migrate SQLite/WAL files, persist sessions, or write
audit events. Runtime-directory writability is inferred from existing metadata,
ownership, access bits, and `os.access`; no probe file is created.

Doctor may use a bounded read-only request to an explicitly configured Unix
socket or loopback Ollama endpoint (`127.0.0.0/8` or `::1`). Redirects and proxy
environment variables are disabled, and the resolved peer must remain
loopback. Every other outbound or non-loopback request is forbidden. Diagnostic
events exist only in memory and disappear when the command exits.

External probes never trust a project-controlled `PATH` or inherited process
environment. Every executable is resolved to an absolute path under an approved
root-owned system location or the configured user-owned Homebrew prefix. The
resolver rejects current-directory/project executables, symlink escapes, and
executables or ancestors writable by an unauthorized group or other users. Each
probe receives a minimal per-tool environment; dynamic-loader, Python, shell,
proxy, and unapproved Git variables are removed. Required values such as
`XDG_RUNTIME_DIR` are individually validated before forwarding.

Git inspection additionally uses `GIT_OPTIONAL_LOCKS=0`, disables hooks and
filesystem monitors through explicit command configuration, avoids aliases,
and uses only read-only subcommands. Doctor tests cover malicious `PATH`,
dynamic-loader variables, executable symlinks, Git hooks, fsmonitor settings,
and repository-local configuration.

Human output is concise and grouped by subsystem. `agent doctor --json`
returns a versioned machine-readable document. Both representations derive from
the same result object so their status cannot diverge.

## Free-Only Guarantee

The core has no paid runtime dependency and requires no cloud account. The
default configuration contains no commercial model or search provider. Phase-1
tests verify that:

- `free_only` is an immutable true invariant; every configuration source that
  attempts to set it false is rejected;
- no API key is required for installation, startup, or doctor checks;
- the package metadata contains only freely usable dependencies;
- disabled or absent GitHub authentication does not break local operation.

While `free_only` is active, remote model, hosted agent, metered search, trial,
subscription, and charge-capable providers are unsupported and rejected even
when configuration or a plugin requests them. Model execution remains local
through Ollama or another explicitly local zero-charge runtime. Public web
resources, package registries, and a free GitHub account may be used for
research and development, but no product path may require payment details,
credits, a trial, or billable usage.

## Hardware Defaults

The foundation records, validates, and reports resource thresholds; it does not
schedule inference yet. Defaults target the current development system:

- Ryzen 9 5900X for tests, indexing, research, and orchestration;
- RTX 2070 SUPER with 8 GiB VRAM for one 7B inference workload;
- 16 GiB system RAM with conservative worker concurrency;
- GPU-first inference with CPU fallback in the later runtime phase;
- optional 14B execution with reduced concurrency, never as the default.

The schema supports hardware overrides without embedding machine-specific
absolute values into portable project configuration. Games and non-agent data
are outside every automatic cleanup scope.

## Error Handling

- Configuration and schema errors fail before state or tool execution.
- Database migrations are atomic and leave the previous schema usable on
  failure.
- Lock contention respects a bounded timeout and returns a diagnostic error.
- Oversized inputs are rejected before policy evaluation; tool timeouts and
  output truncation are explicit result states.
- Doctor probe failures are isolated so one unavailable command cannot suppress
  other results.
- Persistent audit-write failure blocks every future stateful or mutating
  action. Stateless doctor does not open the audit store.
- Unexpected doctor exceptions receive an in-memory incident ID and redacted
  diagnostic. Later stateful workflows record only their event-schema-approved
  exception metadata, without local secrets or raw environment dumps.

## Compatibility and Rollout

Existing commands, tests, state files, release artifacts, and
`safe_agent_v50.py` behavior remain unchanged. The new console script is an
additional entry point. Phase 1 does not redirect `autonomous_agent/main.py` to
the new core.

The existing modular recovery state remains authoritative for the recovery
subsystem. Core SQLite state is authoritative only for new core sessions and
events. No automatic data merge is attempted.

Rollback consists of reverting the phase-1 implementation commit and removing
the optional package installation. The separate XDG SQLite database and audit
head may remain as inert rollback artifacts; legacy code will not read them.

## Testing Strategy

Implementation follows test-driven development. Required test groups are:

1. Configuration precedence, validation, redaction, and fail-closed behavior.
2. Policy decisions for every mode, side-effect class, missing prerequisite,
   trusted/untrusted scope claim, authority grant, and recovery-evidence
   combination.
3. Tool registration, bidirectional dataclass validation, policy gating,
   confirmation non-execution, timeout/process-group termination, byte/depth
   limits, structured failure, and exception conversion.
4. Fresh database creation, ordered migration, transaction rollback, lock
   timeout, file-type checks, and restrictive permissions.
5. Audit hash-chain creation, serialized concurrent append from separate
   processes, lock-file symlink/ownership/mode rejection, verification,
   content mutation, row deletion, tail truncation, competing-chain detection,
   sidecar mismatch, every two-phase crash point, pending rollback/finalization,
   impossible-tail rejection, concurrent writers paused at every transition,
   verifier-versus-writer transitions, and allowlisted secret redaction.
6. Doctor success, partial availability, probe timeout/process termination,
   loopback-only Ollama access, hostile environment/PATH/Git configuration,
   trusted executable resolution, JSON schema, and zero filesystem/state side
   effects using controlled command fakes.
7. Real local smoke tests for `agent doctor` and `agent doctor --json`.
8. Canonical offline legacy gates:
   `./autonomous_agent/tools/test_all_v50.sh` plus the runtime-path, recovery
   storage, and recovery-schema unittest modules. The implementation updates
   both `tools/release_check.sh` and `.github/workflows/release-gate.yml` to run
   these V50/core gates instead of the current V49 or partial-only coverage.
9. `./autonomous_agent/tools/test_end_to_end_v50.sh` as a separately reported
   local hardware/Ollama gate with an explicit skip reason when unavailable;
   it never substitutes for deterministic offline regression tests.

Quality gates include `pytest`, Ruff, MyPy, Bandit, ShellCheck, `shfmt`, Python
compilation, package build and installation into a clean `uv` environment, and
`git diff --check`. Network-independent tests are mandatory; hardware-specific
checks skip with an explicit reason when unavailable.

## Completion Criteria

Phase 1 is complete when:

- a clean environment can install the project and invoke `agent doctor`;
- all configuration sources produce one validated, redacted effective config;
- project configuration cannot widen roots, modes, hard limits, or authority;
- every policy request receives a deterministic allow, confirm, or deny result;
- root and recovery decisions accept only trusted, scoped evidence;
- registered tools cannot bypass policy evaluation;
- core sessions and events survive restart in the versioned SQLite database;
- audit verification detects mutation, sequence/tail changes, and sidecar
  mismatch, and event-schema tests persist no credentials;
- doctor reports the supported local stack in human and JSON formats without
  side effects;
- no paid service, API key, or cloud account is required;
- all new and existing release gates pass;
- tests leave the tracked worktree clean.
