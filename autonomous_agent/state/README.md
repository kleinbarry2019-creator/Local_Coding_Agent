# State Directory

Mutable V50 runtime data is isolated here and ignored by Git.

```text
state/
├── agent_state.json
├── agent_state.json.bak
├── agent_state.sha256
├── audit/
│   ├── audit_log.jsonl
│   └── audit_head.sha256
└── snapshots/
```

On first use, `safe_agent_v50.py` copies legacy runtime files from the
`autonomous_agent/` directory when the isolated destination does not exist.
Legacy files are retained for rollback compatibility.
