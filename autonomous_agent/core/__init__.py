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

__all__ = [
    "DEFAULT_PROBE_NAMES",
    "AgentConfig",
    "CliOverrides",
    "ConfigError",
    "Doctor",
    "DoctorReport",
    "DoctorStatus",
    "ExecutionMode",
    "ProbeResult",
    "ProbeStatus",
    "build_doctor_registry",
    "load_config",
]
