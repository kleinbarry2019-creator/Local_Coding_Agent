"""File-scoped checkpoints with verified rollback through core task state."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from autonomous_agent.core.runtime_tools import ProjectPathResolver
from autonomous_agent.core.task_state import CheckpointRecord, TaskStateStore

_MAX_CHECKPOINT_BYTES = 1_048_576
_MAX_TREE_BYTES = 16_777_216
_MAX_TREE_FILES = 5_000


@dataclass(frozen=True)
class RollbackResult:
    restored: bool
    checkpoint_id: str
    diagnostic: str


class CheckpointManager:
    """Capture and restore exactly one project target around a mutating step."""

    def __init__(self, project_root: Path, state: TaskStateStore) -> None:
        self.paths = ProjectPathResolver(project_root)
        self.state = state

    def create(
        self, session_id: str, step_id: str, target: Path
    ) -> CheckpointRecord:
        path = self.paths.resolve(target, allow_missing=True)
        checkpoint_id = f"checkpoint-{uuid.uuid4().hex}"
        existed = path.exists()
        if existed and path.is_dir():
            return self._create_tree(session_id, step_id, path, checkpoint_id)
        content = b""
        if existed:
            if not path.is_file():
                raise RuntimeError("checkpoint target is not a regular file")
            content = path.read_bytes()
            if len(content) > _MAX_CHECKPOINT_BYTES:
                raise RuntimeError("checkpoint target exceeds the byte limit")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "target": path.relative_to(self.paths.project_root).as_posix(),
            "existed": existed,
            "byte_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        return self.state.create_checkpoint(
            session_id,
            step_id,
            manifest,
            checkpoint_id=checkpoint_id,
        )

    def _create_tree(
        self, session_id: str, step_id: str, root: Path, checkpoint_id: str
    ) -> CheckpointRecord:
        files: dict[str, object] = {}
        total = 0
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if ".git" in relative.parts or path.is_symlink() or not path.is_file():
                continue
            content = path.read_bytes()
            total += len(content)
            if total > _MAX_TREE_BYTES or len(files) >= _MAX_TREE_FILES:
                raise RuntimeError("project checkpoint exceeds its bounded budget")
            files[relative.as_posix()] = {
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        return self.state.create_checkpoint(
            session_id,
            step_id,
            {
                "schema_version": 1,
                "kind": "tree",
                "target": root.relative_to(self.paths.project_root).as_posix() or ".",
                "files": files,
                "total_bytes": total,
            },
            checkpoint_id=checkpoint_id,
        )

    def restore(self, checkpoint: CheckpointRecord) -> RollbackResult:
        manifest = checkpoint.manifest
        if manifest.get("kind") == "tree":
            return self._restore_tree(checkpoint)
        target = manifest.get("target")
        existed = manifest.get("existed")
        encoded = manifest.get("content_base64")
        digest = manifest.get("sha256")
        if (
            type(target) is not str
            or type(existed) is not bool
            or type(encoded) is not str
            or type(digest) is not str
        ):
            return RollbackResult(False, checkpoint.checkpoint_id, "invalid-manifest")
        path = self.paths.resolve(Path(target), allow_missing=True)
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError:
            return RollbackResult(False, checkpoint.checkpoint_id, "invalid-content")
        if hashlib.sha256(content).hexdigest() != digest:
            return RollbackResult(False, checkpoint.checkpoint_id, "digest-mismatch")
        if existed:
            _atomic_write(path, content)
        else:
            path.unlink(missing_ok=True)
        self.state.mark_checkpoint(
            checkpoint.checkpoint_id, checkpoint.session_id, "restored"
        )
        return RollbackResult(True, checkpoint.checkpoint_id, "restored")

    def _restore_tree(self, checkpoint: CheckpointRecord) -> RollbackResult:
        raw_files = checkpoint.manifest.get("files")
        raw_target = checkpoint.manifest.get("target")
        if not isinstance(raw_files, dict) or type(raw_target) is not str:
            return RollbackResult(False, checkpoint.checkpoint_id, "invalid-manifest")
        root = self.paths.resolve(Path(raw_target))
        expected = {str(name) for name in raw_files}
        for path in sorted(root.rglob("*"), reverse=True):
            relative = path.relative_to(root)
            if ".git" in relative.parts or path.is_symlink():
                continue
            if path.is_file() and relative.as_posix() not in expected:
                path.unlink()
        for name, raw in raw_files.items():
            if type(name) is not str or not isinstance(raw, dict):
                return RollbackResult(False, checkpoint.checkpoint_id, "invalid-manifest")
            encoded = raw.get("content_base64")
            digest = raw.get("sha256")
            if type(encoded) is not str or type(digest) is not str:
                return RollbackResult(False, checkpoint.checkpoint_id, "invalid-manifest")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError:
                return RollbackResult(False, checkpoint.checkpoint_id, "invalid-content")
            if hashlib.sha256(content).hexdigest() != digest:
                return RollbackResult(False, checkpoint.checkpoint_id, "digest-mismatch")
            path = self.paths.resolve(root / name, allow_missing=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content)
        self.state.mark_checkpoint(
            checkpoint.checkpoint_id, checkpoint.session_id, "restored"
        )
        return RollbackResult(True, checkpoint.checkpoint_id, "restored")

    def discard(self, checkpoint: CheckpointRecord) -> None:
        self.state.mark_checkpoint(
            checkpoint.checkpoint_id, checkpoint.session_id, "discarded"
        )


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


__all__ = ["CheckpointManager", "RollbackResult"]
