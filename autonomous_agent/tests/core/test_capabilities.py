from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autonomous_agent.core.capabilities import (
    CapabilityRegistry,
    _terminate_process_group,
)
from autonomous_agent.core.config import ExecutionMode, ResourceLimits
from autonomous_agent.core.policy import (
    AuthorityGrant,
    PolicyContext,
    ScopeEvidence,
)
from autonomous_agent.core.system_tools import register_system_tools
from autonomous_agent.core.tools import (
    ExecutionContext,
    SchemaLimits,
    ToolRegistry,
    ToolStatus,
)


def _context(root: Path) -> ExecutionContext:
    return ExecutionContext(
        policy=PolicyContext(
            mode=ExecutionMode.AUTONOMOUS,
            canonical_project_root=root,
            scope=ScopeEvidence(
                project_root=root,
                resolved_targets=(root,),
                resolver_id="trusted-path-resolver-v1",
                valid=True,
            ),
            hard_limits=ResourceLimits(),
            authority=None,
            recovery=None,
            doctor_capabilities=frozenset(
                {"doctor.read", "doctor.ollama.loopback"}
            ),
        ),
        deadline_monotonic=time.monotonic() + 120.0,
        schema_limits=SchemaLimits(max_output_bytes=1_048_576),
        session_id="session-capability",
    )


def test_existing_external_tool_is_discovered_and_version_verified(
    tmp_path: Path,
) -> None:
    capability = CapabilityRegistry(tmp_path).discover("python3")

    assert capability.available
    assert capability.executable is not None
    assert capability.version is not None


def test_project_local_tool_version_is_probed_inside_sandbox(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "host-marker"
    executable = project / ".venv" / "bin" / "evil"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!/bin/sh\nprintf pwned > {marker}\nprintf '%s\\n' safe-version",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    capability = CapabilityRegistry(project).discover("evil")

    assert capability.available
    assert capability.source == "project-sandbox"
    assert capability.version == "safe-version"
    assert not marker.exists()


def test_project_local_symlink_to_untrusted_host_tool_is_not_executed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "host-marker"
    executable = tmp_path / "evil"
    executable.write_text(
        f"#!/bin/sh\nprintf pwned > {marker}\nprintf '%s\\n' unsafe-version",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    project_tool = project / ".venv" / "bin" / "evil"
    project_tool.parent.mkdir(parents=True)
    project_tool.symlink_to(executable)

    capability = CapabilityRegistry(project).discover("evil")

    assert not capability.available
    assert capability.version is None
    assert not marker.exists()


def test_unknown_missing_tool_is_not_installable(tmp_path: Path) -> None:
    result = CapabilityRegistry(tmp_path).ensure("definitely_missing_agent_tool")

    assert not result.installed
    assert result.diagnostic == "untrusted-or-unsupported-tool"


def test_hung_installer_is_terminated_without_waiting_on_inherited_pipes() -> None:
    process = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    _terminate_process_group(process)

    assert process.poll() is not None
    assert time.monotonic() - started < 6.0


def test_missing_qemu_is_researched_against_trusted_catalog(tmp_path: Path) -> None:
    research = CapabilityRegistry(tmp_path).research("qemu-system-x86_64")

    if Path("/run/ostree-booted").is_file():
        assert research.supported
        assert research.source == "trusted-catalog"
        assert research.manager == "rpm-ostree"
        assert research.package == "qemu-system-x86-core"
        assert "reboot" in research.rationale
    else:
        assert research.supported
        assert research.source == "trusted-catalog"
        assert research.manager in {"apt", "dnf", "brew"}
        assert research.package


def test_immutable_qemu_install_reports_reboot_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path("/run/ostree-booted").is_file():
        pytest.skip("immutable-host behavior is not applicable")
    registry = CapabilityRegistry(tmp_path)
    if registry.discover("qemu-system-x86_64").available:
        pytest.skip("qemu is already available")
    monkeypatch.setattr(
        "autonomous_agent.core.capabilities.PrivilegedSystemExecutor.install",
        lambda _self, _recipe: 0,
    )

    result = registry.ensure("qemu-system-x86_64")

    assert not result.installed
    assert result.reboot_required
    assert result.diagnostic == "installed-reboot-required"


def test_system_tool_requires_exact_action_scoped_authority(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    capabilities = CapabilityRegistry(root)
    registry = ToolRegistry()
    register_system_tools(registry, capabilities, root)
    context = _context(root)
    raw = {"name": "python3", "project_root": str(root)}

    denied = registry.execute("system.ensure-tool", raw, context)
    request = registry.policy_request("system.ensure-tool", raw, context)
    now = datetime.now(UTC)
    grant = AuthorityGrant(
        grant_id="grant-capability",
        capabilities=request.capabilities,
        action_digest=request.request_id,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=1),
    )
    allowed = registry.execute(
        "system.ensure-tool",
        raw,
        replace(context, policy=replace(context.policy, authority=grant)),
    )

    assert denied.status is ToolStatus.DENIED
    assert denied.diagnostic_code == "authority_unavailable"
    assert allowed.status is ToolStatus.OK
    assert allowed.data is not None
    assert allowed.data["installed"] is True
    assert allowed.data["version"] is not None
