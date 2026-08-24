"""Action-scoped external tool provisioning on the shared tool boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autonomous_agent.core.capabilities import CapabilityRegistry
from autonomous_agent.core.policy import NetworkKind, SideEffect
from autonomous_agent.core.tools import ExecutionContext, ToolRegistry, ToolSpec


@dataclass(frozen=True)
class EnsureToolInput:
    name: str
    project_root: Path


@dataclass(frozen=True)
class EnsureToolOutput:
    name: str
    installed: bool
    executable: str | None
    version: str | None
    diagnostic: str


def register_system_tools(
    registry: ToolRegistry,
    capabilities: CapabilityRegistry,
    project_root: Path,
) -> None:
    root = project_root.resolve(strict=True)

    def ensure(
        request: EnsureToolInput, context: ExecutionContext
    ) -> EnsureToolOutput:
        del context
        if request.project_root != root:
            raise PermissionError("system tool project scope is invalid")
        result = capabilities.ensure(request.name)
        return EnsureToolOutput(
            name=result.capability.name,
            installed=result.installed,
            executable=(
                None
                if result.capability.executable is None
                else str(result.capability.executable)
            ),
            version=result.capability.version,
            diagnostic=result.diagnostic,
        )

    registry.register(
        ToolSpec(
            name="system.ensure-tool",
            version="1.0.0",
            description="Install one allowlisted external tool and verify its version.",
            input_type=EnsureToolInput,
            output_type=EnsureToolOutput,
            capabilities=frozenset({"system.install-tool"}),
            side_effect=SideEffect.SYSTEM,
            network=NetworkKind.OUTBOUND,
            requires_elevation=True,
            requires_recovery=False,
            default_timeout_s=120.0,
            max_output_bytes=16_384,
            handler=ensure,
            target_resolver=lambda request: (request.project_root,),
        )
    )


__all__ = ["EnsureToolInput", "EnsureToolOutput", "register_system_tools"]
