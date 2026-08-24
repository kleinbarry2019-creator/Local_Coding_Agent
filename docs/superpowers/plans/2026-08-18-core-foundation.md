# Core Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an installable, free-only local-agent core with trusted configuration, deterministic policy decisions, typed tools, XDG SQLite/audit storage, and a side-effect-free `agent doctor` command while preserving V50 behavior.

**Architecture:** Add an independent `autonomous_agent.core` package beside the V50 monolith. Pure dataclass contracts connect configuration, policy, tools, state, audit, and diagnostics; no core module imports a historical agent entry point. Persistent core state lives under XDG state storage, while doctor remains stateless and executes only hardened, read-only probes.

**Tech Stack:** Python 3.12-3.14 standard-library runtime (`argparse`, `dataclasses`, `enum`, `fcntl`, `hashlib`, `http.client`, `ipaddress`, `json`, `pathlib`, `selectors`, `sqlite3`, `subprocess`, `tomllib`); `uv`; Pytest; Ruff; MyPy; Bandit; ShellCheck; shfmt; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-18-core-foundation-design.md`

## Global Constraints

- Start execution from commit `a1e3197e6f13d1220e191ba4d958ec85f08c495d` on `v50-development-v50.6.2`.
- Before Task 1, use `superpowers:using-git-worktrees` to create an isolated `codex/core-foundation` worktree; never implement directly on the development branch.
- Runtime dependencies remain Python-standard-library only; every development/build dependency must be free to use.
- `free_only` and `audit_required` are immutable `True` invariants.
- Default mode is `monitored`; phase 1 has no root-authority provider and always denies unrestricted-root execution.
- Core state defaults to `$XDG_STATE_HOME/local-coding-agent` or `~/.local/state/local-coding-agent`; it never writes to the repository or installed package.
- Project configuration may only reduce soft limits and cannot select mode, project root, state root, authority, hard limits, or free-only behavior.
- Doctor permits only validated local diagnostic subprocesses and a loopback/Unix-socket Ollama request; it performs no persistent write.
- Preserve `safe_agent_v50.py`, `autonomous_agent/main.py`, legacy state paths, and all current V50 entry points.
- Never use a paid, metered, trial, or charge-capable model/search/agent provider.
- Every feature and bug fix follows red-green-refactor: observe the focused test fail for the intended reason, implement the minimum behavior, then rerun focused and regression gates.
- For every unexpected failure, invoke `superpowers:systematic-debugging`, reproduce it, inspect the exact Git/GitHub context, use the relevant installed plugin where callable, correct the root cause, and rerun the original failing command.
- After every task commit, invoke `superpowers:requesting-code-review`; process feedback with `superpowers:receiving-code-review`. Fix Critical and Important findings before the next task.
- For GitHub CI failures, use `github:gh-fix-ci`; use `gh` only where the GitHub connector has no callable coverage.

## File Map

| File | Responsibility |
|---|---|
| `pyproject.toml` | package metadata, `agent` entry point, free dev dependency groups, Ruff/MyPy/Pytest settings |
| `uv.lock` | reproducible free build/test dependencies |
| `autonomous_agent/__init__.py` | package version only |
| `autonomous_agent/__main__.py` | `python -m autonomous_agent` bridge to CLI |
| `autonomous_agent/core/__init__.py` | stable public core exports |
| `autonomous_agent/core/config.py` | trusted path resolution, TOML loading, authority-aware merge, validation, provenance, redaction |
| `autonomous_agent/core/policy.py` | policy enums, untrusted request, trusted context, evidence contracts, deterministic matrix |
| `autonomous_agent/core/tools.py` | bounded dataclass codec, tool specs, registry, policy gating, structured results |
| `autonomous_agent/core/state.py` | secure XDG files, SQLite schema/migrations, transactional session/config state |
| `autonomous_agent/core/events.py` | event allowlists, secret redaction, hash chain, sidecar/lock protocol, crash recovery |
| `autonomous_agent/core/probes.py` | trusted executable resolver, minimal environments, bounded subprocess and loopback clients |
| `autonomous_agent/core/doctor.py` | probe aggregation and versioned report model |
| `autonomous_agent/cli.py` | argparse dispatch, human/JSON rendering, stable exit codes |
| `autonomous_agent/tests/core/` | isolated unit, integration, adversarial, and CLI tests for phase 1 |
| `README.md` | free local install and doctor usage |
| `docs/DEVELOPMENT_RULES.md` | plugin/GitHub error-verification workflow |
| `tools/release_check.sh` | canonical offline V50 and core release gate |
| `.github/workflows/release-gate.yml` | reproducible GitHub release checks for main and development PRs |

---

### Task 1: Package Skeleton and Reproducible Baseline

**Files:**
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `autonomous_agent/__init__.py`
- Create: `autonomous_agent/core/__init__.py`
- Create: `autonomous_agent/tests/core/__init__.py`
- Create: `autonomous_agent/tests/core/test_package.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `autonomous_agent.__version__ == "50.7.0.dev1"`
- Produces: importable `autonomous_agent.core` namespace
- Consumes: no new interfaces

- [ ] **Step 1: Record the exact offline legacy baseline**

Run:

```bash
git rev-parse HEAD
./autonomous_agent/tools/test_all_v50.sh
python3 -m unittest \
  autonomous_agent.tests.test_runtime_paths \
  autonomous_agent.tests.test_recovery_storage \
  autonomous_agent.tests.test_recovery_schema
```

Expected: HEAD is `a1e3197e6f13d1220e191ba4d958ec85f08c495d`; both test commands exit 0. If they do not, stop and debug the baseline before adding phase-1 code.

- [ ] **Step 2: Write the failing package test**

```python
from importlib.metadata import entry_points, requires

import autonomous_agent


def test_package_version_and_console_script() -> None:
    assert autonomous_agent.__version__ == "50.7.0.dev1"
    scripts = {entry.name: entry.value for entry in entry_points(group="console_scripts")}
    assert scripts["agent"] == "autonomous_agent.cli:main"
    assert requires("local-coding-agent") in (None, [])
```

Run:

```bash
python3 -c 'import autonomous_agent; assert autonomous_agent.__version__ == "50.7.0.dev1"'
```

Expected: FAIL with `AttributeError` because `__version__` does not exist.

- [ ] **Step 3: Add package metadata and minimal namespaces**

Create `autonomous_agent/__init__.py`:

```python
"""Local Coding Agent package."""

__version__ = "50.7.0.dev1"
```

Create `autonomous_agent/core/__init__.py` with only a module docstring. Configure `pyproject.toml` with:

```toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "local-coding-agent"
version = "50.7.0.dev1"
description = "Free, local-first coding agent core"
requires-python = ">=3.12,<3.15"
dependencies = []

[project.scripts]
agent = "autonomous_agent.cli:main"

[dependency-groups]
dev = [
  "bandit>=1.9,<2",
  "build>=1.3,<2",
  "mypy>=1.18,<2",
  "pytest>=8.4,<10",
  "ruff>=0.12,<1",
]

[tool.hatch.build.targets.wheel]
packages = ["autonomous_agent"]

[tool.pytest.ini_options]
testpaths = ["autonomous_agent/tests"]
addopts = "-ra"

[tool.ruff]
target-version = "py312"
line-length = 88

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["autonomous_agent.core", "autonomous_agent.cli"]
```

Add `.venv/`, `dist/`, and `*.egg-info/` to `.gitignore` without changing existing ignore rules.

- [ ] **Step 4: Lock dependencies, install, and rerun the test**

Run:

```bash
uv lock
uv sync --frozen --group dev
uv run pytest autonomous_agent/tests/core/test_package.py -v
uv build
```

Expected: package test passes and both wheel and source archive build under `dist/`.

- [ ] **Step 5: Rerun the offline V50 baseline**

Run the two commands from Step 1 again.

Expected: both exit 0 and `git status --short` lists only Task 1 source/lock changes.

- [ ] **Step 6: Commit and review**

```bash
git add pyproject.toml uv.lock .gitignore \
  autonomous_agent/__init__.py autonomous_agent/core/__init__.py \
  autonomous_agent/tests/core/__init__.py \
  autonomous_agent/tests/core/test_package.py
git commit -m "Build installable local agent package"
```

Run the required per-task Superpowers review before Task 2.

---

### Task 2: Trusted Project, Config, and XDG Path Resolution

**Files:**
- Create: `autonomous_agent/core/config.py`
- Create: `autonomous_agent/tests/core/test_config_paths.py`

**Interfaces:**
- Produces: `ConfigError(code: str, field: str, message: str)`
- Produces: `ResolvedPaths(config_file, project_file, project_root, state_root)`
- Produces: `resolve_paths(*, cwd: Path, home: Path, environ: Mapping[str, str], explicit_project_root: Path | None = None, global_state_override: Path | None = None, cli_state_override: Path | None = None, create_state: bool = False) -> ResolvedPaths`
- Consumes: Python-standard-library filesystem APIs only

- [ ] **Step 1: Write failing path-authority tests**

Tests must cover these exact cases:

```python
def test_git_root_is_resolved_before_project_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    paths = resolve_paths(cwd=nested, home=tmp_path, environ={})
    assert paths.project_root == repo.resolve()
    assert paths.project_file == repo / ".local-agent.toml"


def test_relative_xdg_state_home_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="XDG_STATE_HOME"):
        resolve_paths(
            cwd=tmp_path,
            home=tmp_path,
            environ={"XDG_STATE_HOME": "relative/state"},
        )


```

Also test explicit project roots, XDG fallbacks, prevalidated global/CLI state
precedence, symlink ancestors, non-directory components, unsafe global-file
modes, and `create_state=False` producing no filesystem changes. Project TOML
field authority is tested in Task 3, after the TOML loader exists.

Run: `uv run pytest autonomous_agent/tests/core/test_config_paths.py -v`

Expected: FAIL because `autonomous_agent.core.config` does not exist.

- [ ] **Step 2: Implement the exact path contracts**

Start `config.py` with these public types:

```python
@dataclass(frozen=True)
class ConfigError(Exception):
    code: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.field}: {self.message}"


@dataclass(frozen=True)
class ResolvedPaths:
    config_file: Path
    project_file: Path
    project_root: Path
    state_root: Path
```

Implement root discovery by walking `cwd.resolve(strict=True)` upward to the first `.git` file or directory. Validate global config with `lstat`: regular file, current UID, no group/other write, and no symlink. Treat `XDG_STATE_HOME` only as the validated absolute base. Permit a custom state root only from the validated global file value or explicit CLI argument, with CLI taking precedence.

When `create_state=True`, walk from the nearest existing ancestor, reject symlinks and non-directories, create each missing component with mode `0o700`, and revalidate the final path. When false, return the path without creating it.

- [ ] **Step 3: Run focused adversarial tests**

Run:

```bash
uv run pytest autonomous_agent/tests/core/test_config_paths.py -v
uv run ruff check autonomous_agent/core/config.py \
  autonomous_agent/tests/core/test_config_paths.py
uv run mypy autonomous_agent/core/config.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/config.py \
  autonomous_agent/tests/core/test_config_paths.py
git commit -m "Resolve trusted local agent paths"
```

Run the required per-task Superpowers review before Task 3.

---

### Task 3: Authority-Aware Configuration Merge and Redaction

**Files:**
- Modify: `autonomous_agent/core/config.py`
- Create: `autonomous_agent/tests/core/test_config.py`

**Interfaces:**
- Produces: `ExecutionMode`, `ConfigSource`, `FieldProvenance`, `ResourceLimits`, `CliOverrides`, `AgentConfig`
- Produces: `load_config(*, cwd: Path, home: Path, environ: Mapping[str, str], cli: CliOverrides = CliOverrides()) -> AgentConfig`
- Produces: `AgentConfig.redacted_dict() -> dict[str, object]`
- Consumes: `resolve_paths` from Task 2

- [ ] **Step 1: Write failing precedence and authority tests**

Create table-driven tests proving:

```python
@pytest.mark.parametrize(
    ("document", "field"),
    [
        ('[core]\nmode = "autonomous"\n', "mode"),
        ('[core]\nproject_root = "/"\n', "project_root"),
        ('[core]\nstate_dir = "/tmp/state"\n', "state_dir"),
        ('[core]\nfree_only = false\n', "free_only"),
        ('[core]\naudit_required = false\n', "audit_required"),
        ('[limits]\nhard_command_timeout_s = 999\n', "hard_command_timeout_s"),
    ],
)
def test_project_security_widening_is_rejected(
    project: Path, document: str, field: str
) -> None:
    (project / ".local-agent.toml").write_text(document, encoding="utf-8")
    with pytest.raises(ConfigError, match=field):
        load_config(cwd=project, home=project.parent, environ={})
```

Also test built-in/global/project/environment/CLI precedence, lower-only project limits, CLI autonomous activation, CLI refusal of unrestricted-root, unknown fields, type confusion (`True` as integer), secret redaction, immutable provenance, and false free/audit values from every source.

Run: `uv run pytest autonomous_agent/tests/core/test_config.py -v`

Expected: FAIL because configuration models and loader are absent.

- [ ] **Step 2: Add exact configuration models and defaults**

Use these values and field names:

```python
class ExecutionMode(str, Enum):
    MONITORED = "monitored"
    AUTONOMOUS = "autonomous"
    UNRESTRICTED_ROOT = "unrestricted-root"


class ConfigSource(str, Enum):
    BUILTIN = "builtin"
    GLOBAL = "global"
    PROJECT = "project"
    ENVIRONMENT = "environment"
    CLI = "cli"


@dataclass(frozen=True)
class FieldProvenance:
    field: str
    source: ConfigSource
    source_path: str | None


@dataclass(frozen=True)
class ResourceLimits:
    command_timeout_s: float = 10.0
    hard_command_timeout_s: float = 120.0
    max_output_bytes: int = 65_536
    hard_max_output_bytes: int = 1_048_576
    doctor_probe_timeout_s: float = 5.0
    max_cpu_percent: float = 90.0
    min_free_ram_mib: int = 2_048
    max_vram_percent: float = 90.0
    min_free_disk_mib: int = 2_048


@dataclass(frozen=True)
class CliOverrides:
    project_root: Path | None = None
    state_root: Path | None = None
    mode: ExecutionMode | None = None
    command_timeout_s: float | None = None
    max_output_bytes: int | None = None


@dataclass(frozen=True)
class AgentConfig:
    schema_version: int
    mode: ExecutionMode
    paths: ResolvedPaths
    limits: ResourceLimits
    free_only: bool
    audit_required: bool
    provenance: Mapping[str, FieldProvenance]
```

Set schema version 1, monitored mode, free-only true, and audit-required true. Store provenance through `MappingProxyType`. Define an explicit field/source authority table in code; do not infer allowed fields from TOML structure. Reject unauthorized keys before merging. Project and mapped environment limits may only move toward the safer bound; global and trusted CLI values may vary only within immutable hard maxima.

Redacted output includes only declared fields and provenance. Recursively redact normalized key names containing `api_key`, `authorization`, `credential`, `password`, `secret`, or `token`, plus exact sensitive values collected from mapped inputs.

- [ ] **Step 3: Run configuration and path suites**

```bash
uv run pytest autonomous_agent/tests/core/test_config.py \
  autonomous_agent/tests/core/test_config_paths.py -v
uv run ruff check autonomous_agent/core/config.py autonomous_agent/tests/core
uv run mypy autonomous_agent/core/config.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/config.py autonomous_agent/tests/core/test_config.py
git commit -m "Enforce trusted agent configuration"
```

Run the required per-task Superpowers review before Task 4.

---

### Task 4: Deterministic Policy and Trusted Evidence Boundary

**Files:**
- Create: `autonomous_agent/core/policy.py`
- Create: `autonomous_agent/tests/core/test_policy.py`

**Interfaces:**
- Produces: `SideEffect`, `NetworkKind`, `DecisionKind`, `ScopeEvidence`, `AuthorityGrant`, `RecoveryEvidence`, `PolicyRequest`, `PolicyContext`, `PolicyDecision`
- Produces: `build_doctor_context(config: AgentConfig) -> PolicyContext`
- Produces: `evaluate_policy(request: PolicyRequest, context: PolicyContext) -> PolicyDecision`
- Consumes: `AgentConfig`, `ExecutionMode`, and `ResourceLimits` from Task 3

- [ ] **Step 1: Write the complete failing policy matrix**

Use parameterized tests with these required outcomes:

| Mode/action | Evidence | Outcome/code |
|---|---|---|
| monitored fixed doctor read | doctor context | allow / `doctor_read` |
| monitored Ollama loopback | doctor context | allow / `doctor_loopback` |
| monitored arbitrary process | none | confirm / `confirmation_required` |
| monitored outbound network | none | confirm / `confirmation_required` |
| autonomous in-root future write | valid scope | allow / `project_scope` |
| autonomous outside/symlink/mount target | invalid scope | deny / `invalid_scope` |
| autonomous elevation | any | deny / `elevation_forbidden` |
| unrestricted-root in phase 1 | caller claim | deny / `authority_unavailable` |
| destructive system action | authority but no recovery | deny / `recovery_required` |
| any ambiguous metadata | any | deny / `incomplete_request` |

Add explicit tests proving a `PolicyRequest` boolean/dictionary cannot be converted into `AuthorityGrant` or `RecoveryEvidence`, expired/mismatched evidence denies, and request budgets above context ceilings deny.

Run: `uv run pytest autonomous_agent/tests/core/test_policy.py -v`

Expected: FAIL because `policy.py` does not exist.

- [ ] **Step 2: Implement immutable policy types and matrix**

Use these string enums and frozen dataclasses. Keep requested claims and trusted
context separate:

```python
class SideEffect(str, Enum):
    READ_ONLY = "read-only"
    WRITE_PROJECT = "write-project"
    PROCESS = "process"
    SYSTEM = "system"
    DESTRUCTIVE = "destructive"


class NetworkKind(str, Enum):
    NONE = "none"
    LOOPBACK_DIAGNOSTIC = "loopback-diagnostic"
    OUTBOUND = "outbound"


class DecisionKind(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class ScopeEvidence:
    project_root: Path
    resolved_targets: tuple[Path, ...]
    resolver_id: str
    valid: bool


@dataclass(frozen=True)
class AuthorityGrant:
    grant_id: str
    capabilities: frozenset[str]
    action_digest: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RecoveryEvidence:
    checkpoint_id: str
    checkpoint_digest: str
    protected_scope: tuple[Path, ...]
    verified_at: datetime
    expires_at: datetime
    valid: bool


@dataclass(frozen=True)
class PolicyRequest:
    session_id: str
    request_id: str
    capabilities: frozenset[str]
    side_effect: SideEffect
    requested_targets: tuple[Path, ...]
    network: NetworkKind
    privilege_elevation: bool
    destructive: bool
    requested_timeout_s: float
    requested_output_bytes: int


@dataclass(frozen=True)
class PolicyContext:
    mode: ExecutionMode
    canonical_project_root: Path
    scope: ScopeEvidence | None
    hard_limits: ResourceLimits
    authority: AuthorityGrant | None
    recovery: RecoveryEvidence | None
    doctor_capabilities: frozenset[str]


@dataclass(frozen=True)
class PolicyDecision:
    kind: DecisionKind
    code: str
    explanation: str
    timeout_s: float
    max_output_bytes: int
```

`build_doctor_context` supplies only fixed `doctor.*` capabilities and no authority/recovery. `evaluate_policy` checks completeness, budgets, trusted scope, mode, network, elevation, destructiveness, authority, and recovery in that order. Phase 1 never constructs a root grant; types alone do not grant authority.

- [ ] **Step 3: Verify policy and configuration integration**

```bash
uv run pytest autonomous_agent/tests/core/test_policy.py \
  autonomous_agent/tests/core/test_config.py -v
uv run ruff check autonomous_agent/core/policy.py \
  autonomous_agent/tests/core/test_policy.py
uv run mypy autonomous_agent/core/policy.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/policy.py \
  autonomous_agent/tests/core/test_policy.py
git commit -m "Define fail-closed agent policy"
```

Run the required per-task Superpowers review before Task 5.

---

### Task 5: Bounded Dataclass Tool Registry

**Files:**
- Create: `autonomous_agent/core/tools.py`
- Create: `autonomous_agent/tests/core/test_tools.py`

**Interfaces:**
- Produces: `SchemaLimits`, `ExecutionContext`, `ArtifactRef`, `ToolStatus`, `ToolResult`, `ToolSpec`, `ToolRegistry`
- Produces: `decode_dataclass(raw: Mapping[str, object], expected_type: type[InputT], limits: SchemaLimits) -> InputT`
- Produces: `encode_dataclass(value: OutputT, limits: SchemaLimits) -> Mapping[str, object]`
- Produces: `ToolRegistry.register(spec: ToolSpec[InputT, OutputT]) -> None`
- Produces: `ToolRegistry.execute(name: str, raw_input: Mapping[str, object], context: ExecutionContext) -> ToolResult`
- Consumes: `PolicyContext`, `PolicyRequest`, `PolicyDecision`, `evaluate_policy`

- [ ] **Step 1: Write failing codec and registry tests**

Define frozen test dataclasses and prove exact-field decoding, enum/path/nested/list/map support, output validation, and rejection of unknown fields, `Any`, non-optional unions, bool-as-int confusion, excessive bytes, depth, items, and string length.

Add registry tests proving:

```python
def test_confirmation_does_not_call_handler() -> None:
    called = False

    def handler(request: ExampleInput, context: ExecutionContext) -> ExampleOutput:
        nonlocal called
        called = True
        return ExampleOutput(value=request.value)

    result = registry_with_confirm_policy(handler).execute(
        "example", {"value": "safe"}, execution_context()
    )
    assert result.status is ToolStatus.CONFIRMATION_REQUIRED
    assert called is False
```

Also test duplicate names, invalid versions, missing capabilities, deadline expiry before invocation, handler exception conversion, invalid output, serialized output truncation, diagnostic caps, and artifact metadata caps.

Run: `uv run pytest autonomous_agent/tests/core/test_tools.py -v`

Expected: FAIL because `tools.py` does not exist.

- [ ] **Step 2: Implement exact tool contracts**

Use these core shapes:

```python
@dataclass(frozen=True)
class SchemaLimits:
    max_input_bytes: int = 65_536
    max_output_bytes: int = 65_536
    max_depth: int = 8
    max_items: int = 1_000
    max_string_bytes: int = 65_536


@dataclass(frozen=True)
class ExecutionContext:
    policy: PolicyContext
    deadline_monotonic: float
    schema_limits: SchemaLimits


class ToolStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    CONFIRMATION_REQUIRED = "confirmation-required"
    TIMED_OUT = "timed-out"
    INVALID_INPUT = "invalid-input"
    INVALID_OUTPUT = "invalid-output"


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    media_type: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    data: Mapping[str, object] | None
    diagnostic_code: str | None
    diagnostic: str | None
    duration_ms: int
    truncated: bool
    artifacts: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class ToolSpec(Generic[InputT, OutputT]):
    name: str
    version: str
    description: str
    input_type: type[InputT]
    output_type: type[OutputT]
    capabilities: frozenset[str]
    side_effect: SideEffect
    network: NetworkKind
    requires_elevation: bool
    requires_recovery: bool
    default_timeout_s: float
    max_output_bytes: int
    handler: Callable[[InputT, ExecutionContext], OutputT]
```

Support only explicitly enumerated dataclass annotation forms. Encode to canonical JSON-compatible objects, validate the output dataclass, then enforce serialized bounds. `ToolRegistry.execute` builds the policy request from the immutable spec, returns denied/confirmation results without invoking, checks the monotonic deadline, invokes the trusted phase-1 handler, validates output, and converts exceptions into redacted incident diagnostics.

- [ ] **Step 3: Run focused and integrated policy tests**

```bash
uv run pytest autonomous_agent/tests/core/test_tools.py \
  autonomous_agent/tests/core/test_policy.py -v
uv run ruff check autonomous_agent/core/tools.py autonomous_agent/tests/core/test_tools.py
uv run mypy autonomous_agent/core/tools.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/tools.py autonomous_agent/tests/core/test_tools.py
git commit -m "Add bounded policy-gated tools"
```

Run the required per-task Superpowers review before Task 6.

---

### Task 6: Secure SQLite Store and Ordered Migrations

**Files:**
- Create: `autonomous_agent/core/state.py`
- Create: `autonomous_agent/tests/core/test_state.py`

**Interfaces:**
- Produces: `StateError`, `SessionRecord`, `CoreStateStore`
- Produces: `CoreStateStore.initialize() -> None`
- Produces: `CoreStateStore.connection() -> ContextManager[sqlite3.Connection]`
- Produces: `CoreStateStore.load_session(session_id: str) -> SessionRecord | None`
- Produces: `CoreStateStore.verify_schema() -> bool`
- Consumes: secure state root from Task 2 and redacted config snapshots from Task 3

- [ ] **Step 1: Write failing secure-storage tests**

Test fresh creation, restart persistence, schema checksum/order, migration rollback, foreign keys, WAL, bounded lock timeout, `0o600` files, `0o700` directory, and refusal of database/state-directory symlinks, FIFOs, wrong owner, or group/other-writable paths. Use two connections to prove lock timeout produces `StateError(code="database_busy")`.

Run: `uv run pytest autonomous_agent/tests/core/test_state.py -v`

Expected: FAIL because `state.py` does not exist.

- [ ] **Step 2: Implement schema and secure open**

Use this immutable session shape and construct `CoreStateStore` from explicit
database, anchor, and lock paths derived beneath one validated XDG state root:

```python
@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    mode: str
    status: str
    created_at: str
    updated_at: str


class CoreStateStore:
    def __init__(
        self,
        database_path: Path,
        anchor_path: Path,
        lock_path: Path,
        busy_timeout_s: float = 2.0,
    ) -> None:
        self.database_path = database_path
        self.anchor_path = anchor_path
        self.lock_path = lock_path
        self.busy_timeout_s = busy_timeout_s
```

Create migration 1 with this schema:

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    current_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE config_snapshots (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

Before `sqlite3.connect`, securely prepare the `0o600` regular database file
with `os.open(database_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
os.O_CLOEXEC, 0o600)`, validate UID/mode/type using `fstat`, then revalidate
inode/device after SQLite opens it. The `0o700` parent prevents cross-user
swaps; same-user arbitrary rewriting remains outside the phase-1 threat model.
Enable WAL, foreign keys, and a 2-second busy timeout on every connection.
Apply each migration inside `BEGIN IMMEDIATE` and store a SHA-256 checksum of
its exact SQL.

- [ ] **Step 3: Verify state behavior**

```bash
uv run pytest autonomous_agent/tests/core/test_state.py -v
uv run ruff check autonomous_agent/core/state.py autonomous_agent/tests/core/test_state.py
uv run mypy autonomous_agent/core/state.py
uv run bandit -q -r autonomous_agent/core/state.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/state.py autonomous_agent/tests/core/test_state.py
git commit -m "Persist versioned core state"
```

Run the required per-task Superpowers review before Task 7.

---

### Task 7: Locked Tamper-Evident Event Chain

**Files:**
- Create: `autonomous_agent/core/events.py`
- Create: `autonomous_agent/tests/core/test_events.py`

**Interfaces:**
- Produces: `EventError`, `AuditAnchor`, `EventInput`, `EventRecord`, `AuditLog`
- Produces: `AuditVerification(ok, code, sequence, head_hash)`
- Produces: `AuditLog.start_session(session_id: str, mode: ExecutionMode, config: AgentConfig, created_at: str) -> EventRecord`
- Produces: `AuditLog.append(event: EventInput, state_mutation: Callable[[sqlite3.Connection], None] | None = None) -> EventRecord`
- Produces: `AuditLog.verify() -> AuditVerification`
- Produces: `AuditLog.recover_pending() -> AuditVerification`
- Consumes: `CoreStateStore` transaction access and `AgentConfig.redacted_dict()`

- [ ] **Step 1: Write failing audit and crash tests**

Cover genesis, restart, canonical hashes, allowlisted event fields, unknown-field dropping, recursive secret redaction, exact-secret-value redaction, mutation, deletion, reordering, competing hashes, tail truncation, missing head, and same-database state/event atomicity.

Use `multiprocessing` with barriers to test two writers and one verifier at each transition: before pending sidecar, after pending sidecar, after SQLite commit, and after sidecar finalization. Test lock-file symlink, wrong owner, and unsafe mode rejection. Simulate process death at each phase and assert:

- pending without row rolls back to committed;
- pending with exactly matching next row finalizes;
- any unmatched database tail or sequence/hash mismatch fails closed.

Run: `uv run pytest autonomous_agent/tests/core/test_events.py -v`

Expected: FAIL because `events.py` does not exist.

- [ ] **Step 2: Implement allowlists, canonical hashes, and cross-process locking**

Use these immutable event results:

```python
@dataclass(frozen=True)
class EventInput:
    event_id: str
    session_id: str
    event_type: str
    payload: Mapping[str, object]
    created_at: str


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    event_id: str
    session_id: str
    event_type: str
    payload: Mapping[str, object]
    previous_hash: str
    current_hash: str
    created_at: str


@dataclass(frozen=True)
class AuditVerification:
    ok: bool
    code: str
    sequence: int
    head_hash: str
```

Use a sidecar shaped as:

```json
{
  "version": 1,
  "committed": {"sequence": 0, "hash": "GENESIS"},
  "pending": null
}
```

Define event-specific field allowlists for `session.created`, `config.snapshot`, `tool.failed`, and `audit.recovered`. Canonicalize with sorted keys, UTF-8, and compact JSON separators. Hash `sequence`, IDs, timestamps, event type, payload, and previous hash.

Acquire an in-process mutex and exclusive `fcntl.flock` on a validated `0o600` no-follow lock file before any database/head comparison. Hold it across pending sidecar fsync/rename, `BEGIN IMMEDIATE`, event plus state mutation, commit, and final sidecar fsync/rename. Verification and startup recovery acquire the same lock for their complete snapshot.

Implement the exact pending recovery rules from the spec. Do not auto-repair any state not represented by a valid pending anchor. Return stable error codes for lock, anchor, sequence, hash, tail, redaction, and transaction failures.

- [ ] **Step 3: Run audit stress and storage integration tests**

```bash
uv run pytest autonomous_agent/tests/core/test_events.py \
  autonomous_agent/tests/core/test_state.py -v
uv run ruff check autonomous_agent/core/events.py autonomous_agent/tests/core/test_events.py
uv run mypy autonomous_agent/core/events.py
uv run bandit -q -r autonomous_agent/core/events.py
```

Expected: all commands exit 0, including repeated execution of the multiprocessing cases ten times.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/events.py autonomous_agent/tests/core/test_events.py
git commit -m "Add locked audit event chain"
```

Run the required per-task Superpowers review before Task 8.

---

### Task 8: Hardened Diagnostic Probe Runtime

**Files:**
- Create: `autonomous_agent/core/probes.py`
- Create: `autonomous_agent/tests/core/test_probes.py`

**Interfaces:**
- Produces: `ProbeError`, `ProcessResult`, `LoopbackResponse`, `TrustedExecutableResolver`
- Produces: `TrustedExecutableResolver.resolve(name: str) -> Path`
- Produces: `build_probe_environment(tool: str, source: Mapping[str, str]) -> Mapping[str, str]`
- Produces: `run_bounded_process(executable: Path, arguments: tuple[str, ...], environment: Mapping[str, str], deadline_monotonic: float, max_bytes: int) -> ProcessResult`
- Produces: `get_loopback_json(endpoint: str | Path, request_path: str, deadline_monotonic: float, max_bytes: int) -> LoopbackResponse`
- Consumes: `ConfigError` and configured time/output limits

- [ ] **Step 1: Write failing hostile-process tests**

Tests must prove that project/current-directory executables, PATH injection,
symlink escapes, wrong-owner or group/other-writable executables/ancestors,
`LD_PRELOAD`, `PYTHONPATH`, shell variables, proxy variables, Git aliases,
hooks, and fsmonitor cannot influence a probe. Include a fake process that
forks a child and streams unlimited output; assert byte caps and process-group
termination on cap and timeout.

Loopback tests must allow `127.0.0.1`, another `127/8` address, `::1`, and a
validated Unix socket; reject public/private non-loopback IPs, mixed DNS
answers, redirects, proxy use, credentials in URLs, non-HTTP schemes, and
responses larger than the cap.

Run: `uv run pytest autonomous_agent/tests/core/test_probes.py -v`

Expected: FAIL because `probes.py` does not exist.

- [ ] **Step 2: Implement trusted resolution and minimal environments**

Search names only in `/usr/bin`, `/usr/local/bin`, `/bin`, and the `bin`
directory of `/home/linuxbrew/.linuxbrew` or another explicitly validated
Homebrew prefix. After resolving symlinks, require the target to remain beneath
a root-owned system location or the complete validated Homebrew prefix. Require
a regular executable owned by root or the current UID and reject unauthorized
group/other write on the file or approved root. Never search the project
directory or ambient PATH.

Build a new environment per tool. Default keys are `LANG=C.UTF-8` and
`LC_ALL=C.UTF-8`. Validate and add `HOME`, `XDG_RUNTIME_DIR`, or
`DBUS_SESSION_BUS_ADDRESS` only for a probe that requires them. Remove all
loader, Python, shell, proxy, and unapproved Git variables. Git commands add
`GIT_OPTIONAL_LOCKS=0`, `GIT_CONFIG_NOSYSTEM=1`,
`GIT_CONFIG_GLOBAL=/dev/null`, and explicit `-c core.hooksPath=/dev/null`,
`-c core.fsmonitor=false`, and `-c core.untrackedCache=false` controls. Invoke
only built-in Git subcommands, which cannot be replaced by aliases.

Use `subprocess.Popen(argv, shell=False, start_new_session=True,
stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=probe_env, close_fds=True)`
and `selectors` to read stdout/stderr incrementally up to the byte cap. On
deadline or cap, send
`SIGTERM` to the process group, wait a bounded grace interval, then `SIGKILL`
the group and reap the process. Use `http.client` directly for loopback HTTP,
disable redirect/proxy behavior, resolve and validate every address with
`ipaddress.ip_address(address).is_loopback`, and read at most `max_bytes + 1`.

- [ ] **Step 3: Run repeated adversarial tests**

```bash
uv run pytest autonomous_agent/tests/core/test_probes.py -v
for run in 1 2 3 4 5; do
  uv run pytest autonomous_agent/tests/core/test_probes.py -q
done
uv run ruff check autonomous_agent/core/probes.py autonomous_agent/tests/core/test_probes.py
uv run mypy autonomous_agent/core/probes.py
uv run bandit -q -r autonomous_agent/core/probes.py
```

Expected: all repeated runs exit 0 and leave no child process alive.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/probes.py autonomous_agent/tests/core/test_probes.py
git commit -m "Harden local diagnostic probes"
```

Run the required per-task Superpowers review before Task 9.

---

### Task 9: Stateless Doctor Aggregation

**Files:**
- Create: `autonomous_agent/core/doctor.py`
- Create: `autonomous_agent/tests/core/test_doctor.py`

**Interfaces:**
- Produces: `ProbeStatus`, `ProbeResult`, `DoctorStatus`, `DoctorReport`, `Doctor`
- Produces: `Doctor(config: AgentConfig, registry: ToolRegistry, probe_names: tuple[str, ...])`
- Produces: `Doctor.run() -> DoctorReport`
- Produces: `DoctorReport.to_dict() -> dict[str, object]`
- Consumes: Tasks 3-5 and 8; does not import Tasks 6-7

- [ ] **Step 1: Write failing doctor behavior tests**

Use injected fake probes to test healthy, warning, unhealthy, unavailable
optional tool, failed required tool, timeout, truncated output, stable ordering,
stable diagnostic codes, and versioned JSON. Snapshot filesystem state before
and after a run and assert no database, WAL, state directory, session, audit,
Git mutation, service start, or downloaded model appears.

Assert that importing `doctor.py` does not import `state.py` or `events.py` and
that an unexpected probe exception becomes an in-memory incident with no raw
environment, command output, token, or traceback in user data.

Run: `uv run pytest autonomous_agent/tests/core/test_doctor.py -v`

Expected: FAIL because `doctor.py` does not exist.

- [ ] **Step 2: Implement report models and probe inventory**

Use these public report fields:

```python
@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: ProbeStatus
    required: bool
    code: str
    summary: str
    data: Mapping[str, object]
    duration_ms: int
    truncated: bool


@dataclass(frozen=True)
class DoctorReport:
    schema_version: int
    status: DoctorStatus
    generated_at: str
    mode: str
    free_only: bool
    project_root: str
    probes: tuple[ProbeResult, ...]
```

Register probes for OS/architecture/Python, CPU/RAM/swap/disk, Git and hardened
worktree status, `gh --version`, Ollama loopback health and `/api/tags`, NVIDIA
driver/GPU/VRAM, Podman, Bubblewrap, systemd-run, Node/npm, uv, quality tools,
runtime-path metadata, and SQLite version/FTS5. Required probes are Python,
SQLite, Git, project root, and state-path safety metadata; optional probes are
warnings when absent.

Use the Task 5 registry with a doctor policy context. Keep diagnostic events in
memory. Derive overall status as unhealthy when any required probe fails,
warning when only optional probes fail, and healthy otherwise.

- [ ] **Step 3: Verify doctor integration and zero side effects**

```bash
uv run pytest autonomous_agent/tests/core/test_doctor.py \
  autonomous_agent/tests/core/test_probes.py \
  autonomous_agent/tests/core/test_tools.py -v
uv run ruff check autonomous_agent/core/doctor.py autonomous_agent/tests/core/test_doctor.py
uv run mypy autonomous_agent/core/doctor.py
uv run bandit -q -r autonomous_agent/core/doctor.py
```

Expected: all commands exit 0.

- [ ] **Step 4: Commit and review**

```bash
git add autonomous_agent/core/doctor.py autonomous_agent/tests/core/test_doctor.py
git commit -m "Diagnose local agent readiness"
```

Run the required per-task Superpowers review before Task 10.

---

### Task 10: CLI, Install Smoke Test, and User Documentation

**Files:**
- Create: `autonomous_agent/cli.py`
- Create: `autonomous_agent/__main__.py`
- Create: `autonomous_agent/tests/core/test_cli.py`
- Create: `README.md`
- Modify: `autonomous_agent/core/__init__.py`

**Interfaces:**
- Produces: `build_parser() -> argparse.ArgumentParser`
- Produces: `main(argv: Sequence[str] | None = None) -> int`
- Consumes: `load_config`, `Doctor`, and `DoctorReport`

- [ ] **Step 1: Write failing CLI tests**

Test `agent --help`, `agent doctor`, `agent doctor --json`,
`python -m autonomous_agent doctor --json`, explicit project root, monitored and
autonomous CLI modes, unrestricted-root rejection, malformed configuration,
healthy/warning/unhealthy exit codes, deterministic JSON, and no state writes.

Use exit codes 0 for healthy, 1 for warning/unhealthy diagnostic results, 2 for
CLI/configuration errors, and 3 for unexpected internal failures.

Run: `uv run pytest autonomous_agent/tests/core/test_cli.py -v`

Expected: FAIL because CLI modules do not exist.

- [ ] **Step 2: Implement argparse dispatch and rendering**

The parser exposes only this phase-1 surface:

```text
agent doctor [--json] [--project PATH] [--state-dir PATH]
             [--mode monitored|autonomous]
```

`main` catches only declared configuration/diagnostic errors at the boundary,
prints redacted single-line errors to stderr, and returns the defined code.
Human output groups system, development, local model, containment, and policy
results. JSON uses `json.dumps(report.to_dict(), sort_keys=True,
separators=(",", ":"))` with one trailing newline. No CLI path initializes
`CoreStateStore` or `AuditLog`.

Create `__main__.py`:

```python
from autonomous_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

Export only stable phase-1 contracts from `core/__init__.py`.

- [ ] **Step 3: Document the free local workflow**

`README.md` must include:

- Bazzite/Linux prerequisites: Python 3.12-3.14, uv, Git, local Ollama;
- `uv sync --frozen --group dev` and `uv run agent doctor`;
- `uv tool install .` for a user-level `agent` command;
- human and JSON examples;
- monitored default and explicit autonomous mode;
- phase-1 root execution absence;
- immutable free-only/local-model guarantee;
- XDG config/state paths and zero-write doctor guarantee;
- legacy V50 compatibility and links to the design/spec.

- [ ] **Step 4: Run installed-command smoke tests**

```bash
uv run pytest autonomous_agent/tests/core/test_cli.py \
  autonomous_agent/tests/core/test_package.py -v
uv build
uv run --isolated --with dist/*.whl agent --help
uv run agent doctor --json
python3 -m autonomous_agent doctor --json
```

Expected: tests and help exit 0; real doctor emits valid schema-version-1 JSON
and exits 0 or 1 according to optional/required local readiness, never 2 or 3.

- [ ] **Step 5: Commit and review**

```bash
git add autonomous_agent/cli.py autonomous_agent/__main__.py \
  autonomous_agent/core/__init__.py autonomous_agent/tests/core/test_cli.py \
  README.md
git commit -m "Expose local agent doctor CLI"
```

Run the required per-task Superpowers review before Task 11.

---

### Task 11: Release Gates, Plugin Error Workflow, and GitHub Validation

**Files:**
- Modify: `tools/release_check.sh`
- Modify: `.github/workflows/release-gate.yml`
- Modify: `docs/DEVELOPMENT_RULES.md`
- Create: `autonomous_agent/tests/core/test_release_contract.py`

**Interfaces:**
- Produces: one canonical offline release command: `./tools/release_check.sh`
- Produces: GitHub PR checks for `main` and `v50-development-*`
- Consumes: all phase-1 modules and legacy V50 gates

- [ ] **Step 1: Write the failing release-contract test**

Parse the shell and workflow files as text and assert they contain:

```python
REQUIRED_GATE_FRAGMENTS = {
    "tools/release_check.sh": (
        "./autonomous_agent/tools/test_all_v50.sh",
        "autonomous_agent.tests.test_runtime_paths",
        "autonomous_agent.tests.test_recovery_storage",
        "autonomous_agent.tests.test_recovery_schema",
        "uv run pytest autonomous_agent/tests/core",
        "uv run ruff check",
        "uv run mypy",
        "uv build",
    ),
    ".github/workflows/release-gate.yml": (
        '"v50-development-*"',
        "uv sync --frozen --group dev",
        "./tools/release_check.sh",
    ),
}
```

Also assert neither gate invokes `test_all_v49.sh`, imports
`safe_agent_v49`, or requires `test_end_to_end_v50.sh`.

Run: `uv run pytest autonomous_agent/tests/core/test_release_contract.py -v`

Expected: FAIL because current gates still contain V49/partial behavior.

- [ ] **Step 2: Replace the shell gate with canonical offline checks**

Use `set -Eeuo pipefail`. Preserve the initial clean-tree check, then run in
this order:

1. `./autonomous_agent/tools/test_all_v50.sh`
2. the three recovery/runtime unittest modules
3. `uv run pytest autonomous_agent/tests/core`
4. `uv run ruff check autonomous_agent/core autonomous_agent/cli.py autonomous_agent/tests/core`
5. `uv run mypy autonomous_agent/core autonomous_agent/cli.py`
6. `uv run bandit -q -r autonomous_agent/core autonomous_agent/cli.py`
7. `shellcheck tools/release_check.sh autonomous_agent/tools/test_all_v50.sh`
8. `shfmt -d tools/release_check.sh autonomous_agent/tools/test_all_v50.sh`
9. Python compileall for V50 plus all new core/CLI files
10. `uv build`
11. V50 release-file checks and `git diff --check`

Keep live Ollama E2E as a separate documented command and never silently skip
or substitute it inside the offline gate.

- [ ] **Step 3: Make GitHub run the same gate**

Extend the pull-request branch filter with `"v50-development-*"`. Install
Python 3.12 and `uv==0.12.5`, run `uv sync --frozen --group dev`, install
ShellCheck and shfmt with `sudo apt-get update && sudo apt-get install -y
shellcheck shfmt`, then invoke only `./tools/release_check.sh`. Keep existing
release artifact and unsafe-shell checks when they are not already covered by
the script.

- [ ] **Step 4: Document mandatory plugin/GitHub error correction**

Add this sequence to `docs/DEVELOPMENT_RULES.md`:

1. reproduce the exact failure and capture command/commit/check URL;
2. use systematic debugging before proposing a fix;
3. inspect GitHub check logs with the GitHub plugin or `gh` fallback;
4. use the matching installed review/security plugin when callable;
5. write or tighten a regression test before production changes;
6. correct the root cause, rerun the focused command, then the full gate;
7. request independent review and resolve Critical/Important feedback;
8. push, wait for GitHub checks, and verify the reviewed SHA matches local HEAD;
9. report unavailable plugin endpoints instead of claiming they ran.

- [ ] **Step 5: Run focused release-file tests before committing**

```bash
uv run pytest autonomous_agent/tests/core/test_release_contract.py -v
shfmt -d tools/release_check.sh autonomous_agent/tools/test_all_v50.sh
shellcheck tools/release_check.sh autonomous_agent/tools/test_all_v50.sh
git diff --check
```

Expected: all commands exit 0. Do not run the clean-tree release gate until the
task changes have been committed.

- [ ] **Step 6: Commit the release integration**

```bash
git add tools/release_check.sh .github/workflows/release-gate.yml \
  docs/DEVELOPMENT_RULES.md \
  autonomous_agent/tests/core/test_release_contract.py
git commit -m "Gate core foundation releases"
```

- [ ] **Step 7: Run completion gates and request final code review**

```bash
./tools/release_check.sh
./autonomous_agent/tools/test_end_to_end_v50.sh
git status --short
```

Expected: offline gate exits 0. Live Ollama E2E exits 0 on the target machine;
if local hardware/service is unavailable, record the exact explicit skip reason
without weakening the offline gate. `git status --short` is empty.

Invoke `superpowers:requesting-code-review` for the entire base-to-HEAD range.
Apply `superpowers:receiving-code-review` and fix every verified Critical or
Important issue with a regression test and separate commit. Rerun the focused
test, `./tools/release_check.sh`, and the final review after each fix series.

- [ ] **Step 8: Publish a focused draft PR and verify GitHub**

Create `/tmp/local-agent-core-pr.md` with `apply_patch` and this exact body:

```markdown
## Summary

- add the installable modular Core Foundation beside unchanged V50 entry points
- enforce free-only local operation, trusted configuration, and fail-closed policy
- add bounded tools, XDG SQLite state, locked tamper-evident audit, and stateless doctor
- replace stale V49/partial release checks with canonical offline V50 and core gates

## Validation

- `./tools/release_check.sh`
- `./autonomous_agent/tools/test_end_to_end_v50.sh`
- independent Superpowers code review with no unresolved Critical or Important findings

## Security boundaries

- no paid or remote model/agent provider
- no root authority in phase 1
- project configuration cannot widen authority, roots, or hard limits
- doctor allows only hardened local diagnostics and loopback Ollama access

## Rollback

Revert the feature commits. Legacy V50 code and state remain authoritative and
the separate XDG core database can remain as an inert artifact.
```

```bash
git push -u origin codex/core-foundation
gh pr create --draft \
  --base v50-development-v50.6.2 \
  --head codex/core-foundation \
  --title "Build local agent core foundation" \
  --body-file /tmp/local-agent-core-pr.md
gh pr checks --watch
```

Create the body file with a real Markdown summary of architecture, free-only
guarantee, security boundaries, tests, live Ollama result, and rollback. If a
check fails, invoke `github:gh-fix-ci`, inspect logs, reproduce locally, add a
regression test, correct the root cause, push, and wait again. Do not mark the
PR ready until all required checks pass and GitHub's head SHA equals local
`git rev-parse HEAD`.

---

## Final Completion Checklist

- [ ] `uv run pytest autonomous_agent/tests/core -v` passes with zero failures.
- [ ] `./autonomous_agent/tools/test_all_v50.sh` passes.
- [ ] Runtime-path, recovery-storage, and recovery-schema unittests pass.
- [ ] Ruff, MyPy, Bandit, ShellCheck, shfmt, compileall, and `uv build` pass.
- [ ] Installed `agent doctor` human and JSON modes execute without persistent writes.
- [ ] Audit concurrency/crash tests pass repeatedly.
- [ ] No paid provider, API key, or cloud account is required by metadata, config, tests, or docs.
- [ ] Independent final review has no unresolved Critical or Important issue.
- [ ] Draft PR checks pass for the exact local HEAD SHA.
- [ ] Git worktree is clean and rollback is documented in the PR.
