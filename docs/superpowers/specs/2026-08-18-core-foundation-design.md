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

`pyproject.toml` defines the package, the `agent` console script, supported
Python versions, and development dependency groups. Runtime code uses the
Python standard library in this phase. Test, lint, and type-check dependencies
remain development-only and are resolved reproducibly through `uv`.

Core modules may use the centralized runtime-path abstraction, but they must not
import `safe_agent_v50.py`, `main.py`, or other historical entry points. A later
adapter will translate between the new contracts and legacy behavior.

## Configuration

### Sources and precedence

Configuration is merged in this order, with later sources overriding earlier
ones:

1. safe built-in defaults;
2. global user configuration;
3. project configuration;
4. environment variables;
5. explicit command-line options.

The global file is located below the XDG configuration directory. The project
file is `.local-agent.toml` at the resolved project root. The loader uses
`tomllib`; it does not execute configuration code or interpolate shell text.

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
- audit enablement;
- doctor probe timeout;
- free-only dependency policy;
- CPU, RAM, VRAM, and disk warning thresholds.

Unknown fields, invalid types, unsupported schema versions, unsafe state paths,
and contradictory limits fail closed with field-specific errors. User-facing
errors may show field names and safe values but never secret values.

The safe default mode is `monitored`. Configuration alone cannot activate
`unrestricted-root`; a future session-level activation mechanism must provide
that runtime authority.

## Policy Contract

The policy engine evaluates a `PolicyRequest` and returns a `PolicyDecision`.
It does not execute tools.

A request includes:

- session and request identifiers;
- requested capabilities;
- side-effect class;
- project and target scope;
- whether network or privilege elevation is needed;
- whether an action is destructive;
- recovery-checkpoint status;
- configured resource budget.

Decisions are `allow`, `confirm`, or `deny` and include a stable reason code,
human explanation, applicable limits, and any unmet prerequisites.

The phase-1 policy matrix establishes these invariants:

- read-only local inspection is allowed in every mode;
- monitored mode allows registered read-only diagnostic probes but requires
  confirmation for writes, network access, arbitrary process execution, or
  privilege elevation;
- autonomous mode may later allow project-scoped development actions but denies
  privilege elevation and destructive system actions;
- unrestricted-root remains denied unless session authority is present;
- destructive system actions require a verified recovery checkpoint even when
  unrestricted-root authority exists;
- missing or ambiguous metadata produces `deny`, never implicit permission.

Only the read-only doctor tools are executable in phase 1, so tests validate the
future-facing policy matrix without creating a premature system executor.

## Tool Contract

Each `ToolSpec` defines:

- a unique stable name and version;
- a short description;
- typed input and output schemas;
- required capabilities;
- side-effect and risk classifications;
- default timeout and maximum captured output;
- whether network, elevation, or recovery is required;
- a callable handler.

The registry rejects duplicate names, invalid schemas, unbounded output,
non-positive timeouts, and contradictory capability declarations. Discovery is
static in phase 1. Dynamic plugins and MCP adapters arrive in later phases.

Handlers receive validated input and an immutable execution context. They
return a `ToolResult` containing status, structured data, safe diagnostics,
duration, truncation state, and optional artifact references. Expected tool
failures are results rather than uncaught exceptions. Programmer errors may
raise internally but are converted at the registry boundary and audited.

The registry asks the policy engine for a decision before invoking a handler.
It never assembles or evaluates shell commands from model-generated strings.

## State and Audit Storage

Phase 1 introduces a separate `agent_core.sqlite3` database below the
centralized runtime state directory. It does not reuse the incompatible V50 or
modular recovery JSON schemas.

The initial database contains:

- `schema_migrations` for ordered, transactional migrations;
- `sessions` for identity, mode, lifecycle state, and timestamps;
- `events` for append-only structured events and audit hashes;
- `config_snapshots` for redacted effective configuration.

SQLite uses WAL mode, foreign keys, bounded busy timeouts, and explicit
transactions. Database creation rejects symlink destinations and unexpected
file types. Owner-only permissions are applied where the platform supports
them.

Every audit event stores an event ID, UTC timestamp, session ID, event type,
redacted JSON payload, previous hash, and current hash. The hash is calculated
from a canonical representation. Verification starts at the chain genesis and
fails at the first missing, reordered, or modified event.

Secrets are removed before persistence using both known-field redaction and
value-pattern safeguards. Redaction is defense in depth; callers remain
responsible for not placing credentials in event payloads.

FTS5 code search, long-term memory, embeddings, and artifact content storage
are reserved for later phases. The database migration mechanism must allow
those tables to be added without rebuilding phase-1 data.

## Doctor Command

`agent doctor` is the only new end-user workflow in phase 1. It performs
read-only, time-bounded probes for:

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
must not install software, start services, download models, modify Git, or send
network requests.

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

Future phases may add only local or no-cost default providers. A provider that
can incur charges cannot become a required dependency or default execution
path.

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
- Tool timeouts and output truncation are explicit result states.
- Doctor probe failures are isolated so one unavailable command cannot suppress
  other results.
- Audit-write failure blocks any future mutating action. In phase 1 it marks the
  diagnostic session unhealthy.
- Unexpected exceptions are assigned an incident ID and recorded without local
  secrets or raw environment dumps.

## Compatibility and Rollout

Existing commands, tests, state files, release artifacts, and
`safe_agent_v50.py` behavior remain unchanged. The new console script is an
additional entry point. Phase 1 does not redirect `autonomous_agent/main.py` to
the new core.

The existing modular recovery state remains authoritative for the recovery
subsystem. Core SQLite state is authoritative only for new core sessions and
events. No automatic data merge is attempted.

Rollback consists of reverting the phase-1 implementation commit and removing
the optional package installation. The separate SQLite database may remain as
an inert rollback artifact; legacy code will not read it.

## Testing Strategy

Implementation follows test-driven development. Required test groups are:

1. Configuration precedence, validation, redaction, and fail-closed behavior.
2. Policy decisions for every mode, side-effect class, missing prerequisite,
   and recovery-checkpoint combination.
3. Tool registration, schema rejection, policy gating, timeout, truncation,
   structured failure, and exception conversion.
4. Fresh database creation, ordered migration, transaction rollback, lock
   timeout, file-type checks, and restrictive permissions.
5. Audit hash-chain creation, verification, tamper detection, reordering, and
   secret redaction.
6. Doctor success, partial availability, probe timeout, JSON schema, and zero
   side effects using controlled command fakes.
7. Real local smoke tests for `agent doctor` and `agent doctor --json`.
8. Full V50 and modular recovery regressions with a clean tracked worktree.

Quality gates include `pytest`, Ruff, MyPy, Bandit, ShellCheck, `shfmt`, Python
compilation, package build and installation into a clean `uv` environment, and
`git diff --check`. Network-independent tests are mandatory; hardware-specific
checks skip with an explicit reason when unavailable.

## Completion Criteria

Phase 1 is complete when:

- a clean environment can install the project and invoke `agent doctor`;
- all configuration sources produce one validated, redacted effective config;
- every policy request receives a deterministic allow, confirm, or deny result;
- registered tools cannot bypass policy evaluation;
- core sessions and events survive restart in the versioned SQLite database;
- audit verification detects mutation and secret tests persist no credentials;
- doctor reports the supported local stack in human and JSON formats without
  side effects;
- no paid service, API key, or cloud account is required;
- all new and existing release gates pass;
- tests leave the tracked worktree clean.
