"""Stable phase-1 contracts for the modular local agent core."""

from autonomous_agent.core.config import (
    AgentConfig,
    CliOverrides,
    ConfigError,
    ExecutionMode,
    load_config,
)
from autonomous_agent.core.doctor import (
    DEFAULT_PROBE_NAMES,
    Doctor,
    DoctorReport,
    DoctorStatus,
    ProbeResult,
    ProbeStatus,
    build_doctor_registry,
)
from autonomous_agent.core.preferences import (
    KNOWLEDGE_LEVELS,
    PRONOUN_MODES,
    RESPONSE_STYLES,
    THEMES,
    ProfileStore,
    UserPreferences,
)

__all__ = [
    "DEFAULT_PROBE_NAMES",
    "KNOWLEDGE_LEVELS",
    "PRONOUN_MODES",
    "RESPONSE_STYLES",
    "THEMES",
    "AgentConfig",
    "CliOverrides",
    "ConfigError",
    "Doctor",
    "DoctorReport",
    "DoctorStatus",
    "ExecutionMode",
    "ProbeResult",
    "ProbeStatus",
    "ProfileStore",
    "UserPreferences",
    "build_doctor_registry",
    "load_config",
]
