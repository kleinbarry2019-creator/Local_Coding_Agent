# Local Coding Agent

Local Coding Agent is a free, local-first foundation for a high-quality coding
agent. Phase 1 provides one new end-user workflow: a stateless readiness check
for the local development, containment, and Ollama stack. Existing V50 behavior
remains available and unchanged.

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

Phase 1 deliberately has no unrestricted-root execution mode and `agent
doctor` does not execute coding tasks. The free-only invariant is immutable:
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

The new package and console script are additive. Legacy `safe_agent_v50.py`,
its state, recovery behavior, tests, and release artifacts remain compatible;
phase 1 does not redirect the legacy entry point or migrate its data.

The current contracts and rollout are documented in the
[phase-1 design](docs/superpowers/specs/2026-08-18-core-foundation-design.md) and
[implementation plan](docs/superpowers/plans/2026-08-18-core-foundation.md).
