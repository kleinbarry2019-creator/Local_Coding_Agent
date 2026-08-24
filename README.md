# Local Coding Agent

Local Coding Agent is a free, local-first coding and system agent. It combines
the hardened V50 sandbox with the modular policy, typed-tool, persistent-state,
audit, recovery, doctor, and Ollama foundation. Existing V50 behavior remains
available while the installable CLI owns the consolidated runtime path.

## Requirements

On Bazzite or another Linux distribution, install these free prerequisites:

- Python 3.12, 3.13, or 3.14;
- [uv](https://docs.astral.sh/uv/);
- Git;
- a local [Ollama](https://ollama.com/) installation.

No API key, cloud account, subscription, trial, or payment method is required.

## Development setup

Clone the repository, enter it, and create the reproducible development
environment:

```bash
uv sync --frozen --group dev
uv run agent doctor
```

For a user-level `agent` command, install the checked-out package with uv:

```bash
uv tool install .
agent doctor
```

The default human report is grouped into system, development, local model,
containment, and policy sections. A typical abbreviated result looks like:

```text
Agent doctor: warning
System:
  [PASS] doctor.python: Python is supported.
Development:
  [PASS] doctor.git: Git is available.
Local model:
  [WARNING] doctor.ollama-health: Ollama is unavailable.
Containment:
Policy:
  [PASS] mode: monitored
  [PASS] free-only: enabled
```

Automation can request the deterministic schema-version-1 JSON document:

```bash
agent doctor --json
python3 -m autonomous_agent doctor --json
```

## Autonomous tasks

Simple German or English requests can be executed in autonomous mode. Every
request is normalized into explicit acceptance criteria, persisted in the core
SQLite state, executed through the shared policy/tool boundary, and then
independently rechecked:

```bash
agent run 'Erstelle `result.txt` mit dem Inhalt `verified`'
agent run 'Run python3 -m pytest' --json
agent resume session-0123456789abcdef
```

Supported deterministic intents are file write/read/list, sandboxed process
execution, and allowlisted external-tool installation. Processes run without a
shell in a networkless Bubblewrap sandbox. Missing executable capabilities are
detected automatically. Installation is restricted to a fixed tool/package
catalog, uses a short-lived action digest, spawns at most one non-interactive
privileged child when needed, and verifies the installed executable/version.
The agent process itself refuses to act as a permanent root process.

`completed` is emitted only when every derived acceptance criterion passes,
the action was really executed, and a public-boundary E2E recheck matches the
original request. A successful process exit by itself is only one criterion.

Example (shown on multiple lines here only for readability):

```json
{
  "free_only": true,
  "generated_at": "2026-08-24T00:00:00Z",
  "mode": "monitored",
  "probes": [
    {
      "code": "doctor.ollama-health.unavailable",
      "data": {},
      "duration_ms": 1,
      "name": "doctor.ollama-health",
      "required": false,
      "status": "warning",
      "summary": "Ollama is unavailable.",
      "truncated": false
    }
  ],
  "project_root": "/path/to/project",
  "schema_version": 1,
  "status": "warning"
}
```

The JSON command writes exactly one document to standard output. Exit status 0
means healthy, 1 means warning or unhealthy, 2 means invalid command or
configuration, and 3 means an unexpected internal diagnostic failure.

## Modes and safety boundaries

`monitored` is the safe default. The diagnostic policy can be selected
explicitly for a future autonomous session without enabling agent execution:

```bash
agent doctor --mode autonomous
```

There is no unrestricted-root execution mode and `agent doctor` never executes
coding tasks. The free-only invariant is immutable:
runtime model checks stay local through Ollama, and paid, metered, trial, or
charge-capable providers cannot be enabled by configuration.

Doctor is read-only and zero-write. It does not create configuration or state
directories, SQLite/WAL files, sessions, or audit events. Its default paths are:

- configuration: `$XDG_CONFIG_HOME/local-coding-agent/config.toml`, falling
  back to `$HOME/.config/local-coding-agent/config.toml`;
- state: `$XDG_STATE_HOME/local-coding-agent`, falling back to
  `$HOME/.local/state/local-coding-agent`.

An explicit absolute project can be inspected with `--project PATH`; an
existing or future absolute state location can be inspected with `--state-dir
PATH`. The paths are validated but never created by doctor.

## Compatibility and design

Legacy `safe_agent_v50.py`, its state, recovery behavior, tests, and release
artifacts remain compatible. The active installable CLI uses the modular core;
legacy scripts are compatibility surfaces, not a second active runtime.

The current contracts and rollout are documented in the
[phase-1 design](docs/superpowers/specs/2026-08-18-core-foundation-design.md) and
[implementation plan](docs/superpowers/plans/2026-08-18-core-foundation.md).
