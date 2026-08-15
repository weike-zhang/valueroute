from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from valueroute.settings import ensure_storage_capacity
from valueroute.storage.artifacts import ArtifactRef, ArtifactStore


class CheckpointError(RuntimeError):
    """Raised when a checkpoint is malformed, corrupt, or references bad data."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CheckpointError("checkpoint_not_json_serializable") from error


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Structured recovery facts only; never a provider's private reasoning state."""

    id: str
    boundary_version: int
    owner_version: int
    confirmed_facts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    leases: list[dict[str, Any]] = field(default_factory=list)
    modification_summary: str = ""
    executed_commands: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    recent_failures: list[str] = field(default_factory=list)
    next_step: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    safe_to_resume: bool = False
    artifact_refs: list[ArtifactRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["artifact_refs"] = [reference.to_dict() for reference in self.artifact_refs]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Checkpoint:
        try:
            checkpoint = cls(
                id=value["id"],
                boundary_version=value["boundary_version"],
                owner_version=value["owner_version"],
                confirmed_facts=value.get("confirmed_facts", []),
                open_questions=value.get("open_questions", []),
                resources=value.get("resources", []),
                leases=value.get("leases", []),
                modification_summary=value.get("modification_summary", ""),
                executed_commands=value.get("executed_commands", []),
                evidence_refs=value.get("evidence_refs", []),
                recent_failures=value.get("recent_failures", []),
                next_step=value.get("next_step", ""),
                usage=value.get("usage", {}),
                safe_to_resume=value.get("safe_to_resume", False),
                artifact_refs=[ArtifactRef.from_dict(item) for item in value.get("artifact_refs", [])],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CheckpointError("invalid_checkpoint") from error
        if not checkpoint.id or "/" in checkpoint.id or "\\" in checkpoint.id:
            raise CheckpointError("invalid_checkpoint_id")
        if checkpoint.boundary_version < 1 or checkpoint.owner_version < 1:
            raise CheckpointError("invalid_checkpoint_version")
        if not isinstance(checkpoint.safe_to_resume, bool):
            raise CheckpointError("invalid_checkpoint")
        return checkpoint


class CheckpointStore:
    format_version = 1

    def __init__(self, root: Path, artifact_store: ArtifactStore | None = None, *, max_bytes: int | None = None, min_free_bytes: int = 0):
        self.root = Path(root)
        self.checkpoints_root = self.root / "checkpoints"
        self.artifact_store = artifact_store or ArtifactStore(self.root)
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes

    def save(self, checkpoint: Checkpoint | dict[str, Any]) -> None:
        checkpoint = self._coerce_checkpoint(checkpoint)
        for reference in checkpoint.artifact_refs:
            self.artifact_store.verify(reference)
        payload = checkpoint.to_dict()
        envelope = {
            "format_version": self.format_version,
            "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
            "checkpoint": payload,
        }
        encoded = _canonical_json(envelope)
        ensure_storage_capacity(self.root, incoming_bytes=len(encoded), max_bytes=self.max_bytes, min_free_bytes=self.min_free_bytes)
        _atomic_write(self._path_for(checkpoint.id), encoded)

    def load(self, checkpoint_id: str) -> Checkpoint:
        path = self._path_for(checkpoint_id)
        try:
            envelope = json.loads(path.read_bytes())
            payload = envelope["checkpoint"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise CheckpointError(f"checkpoint_unreadable: {checkpoint_id}") from error
        if envelope.get("format_version") != self.format_version:
            raise CheckpointError("unsupported_checkpoint_format")
        if envelope.get("sha256") != hashlib.sha256(_canonical_json(payload)).hexdigest():
            raise CheckpointError(f"checkpoint_integrity_mismatch: {checkpoint_id}")
        checkpoint = self._coerce_checkpoint(payload)
        if checkpoint.id != checkpoint_id:
            raise CheckpointError("checkpoint_id_mismatch")
        for reference in checkpoint.artifact_refs:
            self.artifact_store.verify(reference)
        return checkpoint

    def list_ids(self) -> list[str]:
        self.checkpoints_root.mkdir(parents=True, exist_ok=True)
        return sorted(path.stem for path in self.checkpoints_root.glob("*.json"))

    def list_valid(self) -> list[Checkpoint]:
        valid: list[Checkpoint] = []
        for checkpoint_id in self.list_ids():
            try:
                valid.append(self.load(checkpoint_id))
            except CheckpointError:
                continue
        return valid

    def _coerce_checkpoint(self, checkpoint: Checkpoint | dict[str, Any]) -> Checkpoint:
        return checkpoint if isinstance(checkpoint, Checkpoint) else Checkpoint.from_dict(checkpoint)

    def _path_for(self, checkpoint_id: str) -> Path:
        if not checkpoint_id or "/" in checkpoint_id or "\\" in checkpoint_id:
            raise CheckpointError("invalid_checkpoint_id")
        return self.checkpoints_root / f"{checkpoint_id}.json"


LocalCheckpointStore = CheckpointStore
