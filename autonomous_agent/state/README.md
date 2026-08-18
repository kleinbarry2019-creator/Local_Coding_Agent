# State Directory

Mutable V50 runtime data is isolated here and ignored by Git.

```text
state/
├── agent_state.json
├── agent_state.json.bak
├── agent_state.sha256
├── recovery_state.json
├── audit/
│   ├── audit_log.jsonl
│   ├── audit_head.sha256
│   └── recovery_audit.json
└── snapshots/
    └── recovery_history.json
```

On first use, `safe_agent_v50.py` copies its legacy runtime files from the
`autonomous_agent/` directory when an isolated destination does not exist.
Default modular `StateManager()` instances separately copy `agent_state.json`,
`agent_history.json`, and `agent_audit.json` from the working directory into
the recovery-specific destinations. Existing isolated destinations are
authoritative. Legacy files are retained unchanged for rollback compatibility;
divergent copies are never merged automatically.
