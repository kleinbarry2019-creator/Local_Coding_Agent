# Autonomy Runtime Capability Matrix

This matrix records the pre-implementation inventory and the consolidation
decision for the autonomous runtime phase.

| Capability | V50 / legacy evidence | Core / foundation evidence | Phase decision |
|---|---|---|---|
| Natural-language goal handling | V50 strict model action JSON; placeholder `autonomy_engine` | No task interpreter | Add deterministic German/English `GoalNormalizer`; preserve Ollama as local readiness/model boundary |
| Planner | `planner.py` emits placeholder analyze/execute/verify steps | No runtime planner | Replace active path with typed `core.autonomy.Planner`; legacy module remains compatibility-only |
| Persistent task state | JSON `StateManager`; V50 hashed state | Versioned, owner-only SQLite `CoreStateStore` | Extend the core schema by immutable migration 2; all task/checkpoint changes use `AuditLog` transactions |
| Audit | V50 hash chain and modular recovery audit | Locked SQLite hash chain plus external anchor | Reuse core `AuditLog`; add narrow allowlists for task, checkpoint, and capability events |
| Policy | V50 mission policy lock | Fail-closed typed `PolicyRequest`/`PolicyContext` | Reuse core policy; add action-scoped authority-grant allowance while continuing to reject unrestricted root |
| Tool registry/runtime | Three incompatible legacy registries/loaders, one currently broken | Typed bounded `ToolRegistry` | Use the core registry as the only active handler invocation boundary; legacy loaders are inactive compatibility code |
| Filesystem execution | V50 runtime-only read/write with symlink checks | Doctor-only tools | Add bounded project tools to the core registry with canonical scope and symlink rejection |
| Command execution | V50 Python-only Bubblewrap runner | Bounded zero-write probes | Reuse V50 containment principles in a tokenized, shell-free, networkless project process tool |
| Capability discovery | Legacy dynamic Python-module discovery | Doctor executable probes | Add fixed-root executable/version discovery; do not import arbitrary discovered code |
| Missing-tool installation | Placeholder `tool_bootstrap` only | Policy models system authority but phase 1 supplied no provider | Add fixed package catalog, exact action digest, short-lived authority, one privileged child, and post-install version verification |
| Privilege boundary | No permanent root design | Policy previously denied all elevation | Keep the agent unprivileged; allow only exact catalog recipes through the short-lived executor; unrestricted-root mode remains denied |
| Checkpoint / rollback | JSON snapshots and recovery chain | Core recovery evidence types | Add file-scoped, hash-verified checkpoints persisted through core state; restore before retry/stop |
| Crash recovery | Modular recovery controller and state snapshots | Durable sessions and crash-safe audit transition | Resume persisted pending/running/recovering/failed tasks from audited step index; completed tasks are immutable |
| Self-healing | `SelfHealer` restores one JSON backup | No task failure loop | Add failure category, root cause, retryability, failure fingerprint, loop detector, bounded replanner, rollback and retry |
| Completion | Legacy loop accepts model `done`; placeholder orchestrator always sets completed | No task completion evaluator | Central `CompletionEvaluator` requires execution, every criterion, independent readback/version/exit evidence, and public-boundary E2E verification |
| Doctor / Probe / Ollama | V50 direct Ollama use | Hardened zero-write doctor and bounded Ollama loopback probes | Reuse unchanged; doctor remains zero-write and imports no runtime state |
| Release gate | V50 sandbox/security gate | Core pytest, Ruff, MyPy, Bandit, shell, build checks | Extend core suite with runtime, recovery, CLI and real Bubblewrap E2E tests; retain canonical gate order |

## Active architecture

`acb run` and `acb resume` are the canonical installable task entry points.
The legacy `agent` command remains a compatibility alias. They
flow through goal normalization, typed planning, audited core task state, the
shared typed registry, bounded self-healing, and the central completion
evaluator. `safe_agent_v50.py` remains a tested compatibility artifact. The old
prototype orchestrator, execution engine, dynamic tool loader, and JSON healer
are not called by the installable runtime.

## Completion invariant

A task may transition to `completed` only when all of these are true:

1. Every planned step has real success evidence.
2. Every acceptance criterion derived from the original request passes.
3. A direct public-boundary recheck passes (file readback, typed output,
   sandbox exit observation, or executable/version discovery).
4. The complete report is stored in the audited task transition.

Build success, an exit status of zero, or a unit-test result alone cannot
satisfy this invariant.
