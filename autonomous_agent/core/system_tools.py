"""Action-scoped external tool provisioning on the shared tool boundary."""

from __future__ import annotations

import os
import re
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
    research_source: str | None = None
    package_manager: str | None = None
    package: str | None = None
    reboot_required: bool = False


@dataclass(frozen=True)
class VmPreflightInput:
    project_root: Path


@dataclass(frozen=True)
class VmPreflightOutput:
    ready: bool
    qemu_available: bool
    qemu_version: str | None
    kvm_available: bool
    cpu_count: int
    memory_kib: int
    iommu_groups: int
    gpu_devices: int
    iso_candidates: list[str]
    missing: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class HostSecurityFinding:
    check: str
    severity: str
    target: str
    message: str


@dataclass(frozen=True)
class HostSecurityScanInput:
    project_root: Path


@dataclass(frozen=True)
class HostSecurityScanOutput:
    clean: bool
    checks: list[str]
    findings: list[HostSecurityFinding]
    listeners: list[str]
    processes_scanned: int


@dataclass(frozen=True)
class ResearchGoalInput:
    goal: str
    project_root: Path


@dataclass(frozen=True)
class ResearchGoalOutput:
    research_completed: bool
    goal_class: str
    supported_workflows: list[str]
    missing_capabilities: list[str]
    blockers: list[str]
    next_steps: list[str]
    browser_opened: bool
    network_used: bool


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
        research = capabilities.research(request.name)
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
            research_source=research.source,
            package_manager=research.manager,
            package=research.package,
            reboot_required=result.reboot_required,
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

    def host_security_scan(
        request: HostSecurityScanInput, context: ExecutionContext
    ) -> HostSecurityScanOutput:
        del context
        if request.project_root != root:
            raise PermissionError("host security project scope is invalid")
        checks = [
            "kernel.randomize_va_space",
            "fs.protected_hardlinks",
            "fs.protected_symlinks",
            "process-command-lines",
            "tcp-listeners",
        ]
        findings: list[HostSecurityFinding] = []
        for setting in checks[:3]:
            path = Path("/proc/sys") / Path(setting.replace(".", "/"))
            try:
                value = path.read_text(encoding="ascii").strip()
            except OSError:
                findings.append(
                    HostSecurityFinding(
                        setting,
                        "info",
                        str(path),
                        "Schutzwert konnte nicht gelesen werden; manuell prüfen.",
                    )
                )
                continue
            expected = "2" if setting == "kernel.randomize_va_space" else "1"
            if value != expected:
                findings.append(
                    HostSecurityFinding(
                        setting,
                        "medium",
                        str(path),
                        f"Erwarteter Schutzwert {expected}, beobachtet wurde {value!r}.",
                    )
                )
        listeners = _host_tcp_listeners()
        processes_scanned = 0
        suspicious = re.compile(
            r"(?i)(?:curl|wget)\b[^\n|]*\|\s*(?:sh|bash)|\bnc\b[^\n]*\s-e\b"
        )
        for process in sorted(Path("/proc").glob("[0-9]*")):
            try:
                raw = (process / "cmdline").read_bytes()
            except OSError:
                continue
            processes_scanned += 1
            command = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")[:512]
            if suspicious.search(command):
                findings.append(
                    HostSecurityFinding(
                        "process-command-lines",
                        "high",
                        process.name,
                        "Verdächtiges Download- oder Reverse-Shell-Muster erkannt; nicht automatisch beendet.",
                    )
                )
                if len(findings) >= 128:
                    break
        return HostSecurityScanOutput(
            clean=not findings,
            checks=checks,
            findings=findings[:128],
            listeners=list(listeners[:64]),
            processes_scanned=processes_scanned,
        )

    registry.register(
        ToolSpec(
            name="system.host-security-scan",
            version="1.0.0",
            description="Inspect bounded local host security signals without changing the system.",
            input_type=HostSecurityScanInput,
            output_type=HostSecurityScanOutput,
            capabilities=frozenset({"system.security-audit"}),
            side_effect=SideEffect.READ_ONLY,
            network=NetworkKind.NONE,
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=10.0,
            max_output_bytes=65_536,
            handler=host_security_scan,
            target_resolver=lambda request: (request.project_root,),
        )
    )


    def _host_tcp_listeners() -> tuple[str, ...]:
        listeners: set[str] = set()
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(name).read_text(encoding="ascii").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 4 or fields[3] != "0A":
                    continue
                try:
                    port = int(fields[1].rsplit(":", 1)[1], 16)
                except (IndexError, ValueError):
                    continue
                listeners.add(f"{name.rsplit('/', 1)[-1]}:{port}")
        return tuple(sorted(listeners))

    def research_goal(
        request: ResearchGoalInput, context: ExecutionContext
    ) -> ResearchGoalOutput:
        del context
        if request.project_root != root:
            raise PermissionError("goal research project scope is invalid")
        lowered = request.goal.casefold()
        is_api = "api" in lowered or "microservice" in lowered
        known = tuple(item.name for item in capabilities.snapshot())
        supported_workflows = [
            "project.read-file",
            "project.write-file",
            "project.run-process",
            "project.analyze",
        ]
        missing = ["complex-code-generation"] if is_api else []
        blockers = [
            "No generic code-generation model is configured in this local runtime; execution is not claimed.",
        ] if is_api else [
            "The task requires a bounded implementation plan before execution can be authorized.",
        ]
        next_steps = [
            "Continue trusted background research and store a reviewable proposal.",
            "Create a concrete file/dependency plan and verify each acceptance criterion independently.",
        ]
        if known:
            next_steps.append("Reuse discovered local capabilities: " + ", ".join(known[:8]))
        return ResearchGoalOutput(
            research_completed=True,
            goal_class="web-api" if is_api else "complex-unknown",
            supported_workflows=supported_workflows,
            missing_capabilities=missing,
            blockers=blockers,
            next_steps=next_steps,
            browser_opened=False,
            network_used=False,
        )

    registry.register(
        ToolSpec(
            name="system.research-goal",
            version="1.0.0",
            description="Research an unfamiliar complex goal from local capabilities without opening a browser.",
            input_type=ResearchGoalInput,
            output_type=ResearchGoalOutput,
            capabilities=frozenset({"system.goal-research"}),
            side_effect=SideEffect.READ_ONLY,
            network=NetworkKind.NONE,
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=5.0,
            max_output_bytes=32_768,
            handler=research_goal,
            target_resolver=lambda request: (request.project_root,),
        )
    )
    def vm_preflight(
        request: VmPreflightInput, context: ExecutionContext
    ) -> VmPreflightOutput:
        del context
        if request.project_root != root:
            raise PermissionError("system preflight project scope is invalid")
        qemu = capabilities.discover("qemu-system-x86_64")
        kvm = Path("/dev/kvm")
        kvm_ready = kvm.is_char_device() and os.access(kvm, os.R_OK | os.W_OK)
        iommu_root = Path("/sys/kernel/iommu_groups")
        iommu_groups = (
            len(tuple(iommu_root.glob("[0-9]*"))) if iommu_root.is_dir() else 0
        )
        gpu_devices = tuple(Path("/sys/class/drm").glob("card[0-9]"))
        iso_candidates = tuple(
            sorted(
                item.name
                for item in root.iterdir()
                if item.is_file() and item.suffix.casefold() == ".iso"
            )
        )
        memory_kib = 0
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    memory_kib = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            memory_kib = 0
        missing: list[str] = []
        if not qemu.available or qemu.version is None:
            missing.append("qemu-system-x86_64")
        if not kvm_ready:
            missing.append("writable /dev/kvm")
        if not iso_candidates:
            missing.append("Windows installation ISO in the project folder")
        if iommu_groups == 0:
            missing.append("IOMMU groups for GPU passthrough")
        warnings = [
            "GPU passthrough requires a dedicated GPU and VFIO binding; the host display GPU must remain available.",
            "A Windows license and user-approved VM disk size are required before creation.",
        ]
        return VmPreflightOutput(
            ready=not missing,
            qemu_available=qemu.available,
            qemu_version=qemu.version,
            kvm_available=kvm_ready,
            cpu_count=os.cpu_count() or 0,
            memory_kib=memory_kib,
            iommu_groups=iommu_groups,
            gpu_devices=len(gpu_devices),
            iso_candidates=list(iso_candidates),
            missing=missing,
            warnings=warnings,
        )

    registry.register(
        ToolSpec(
            name="system.vm-preflight",
            version="1.0.0",
            description="Inspect bounded virtualization and Windows VM prerequisites without changing the host.",
            input_type=VmPreflightInput,
            output_type=VmPreflightOutput,
            capabilities=frozenset({"system.virtualization-preflight"}),
            side_effect=SideEffect.READ_ONLY,
            network=NetworkKind.NONE,
            requires_elevation=False,
            requires_recovery=False,
            default_timeout_s=5.0,
            max_output_bytes=16_384,
            handler=vm_preflight,
            target_resolver=lambda request: (request.project_root,),
        )
    )


__all__ = [
    "EnsureToolInput",
    "EnsureToolOutput",
    "HostSecurityFinding",
    "HostSecurityScanInput",
    "HostSecurityScanOutput",
    "ResearchGoalInput",
    "ResearchGoalOutput",
    "VmPreflightInput",
    "VmPreflightOutput",
    "register_system_tools",
]
