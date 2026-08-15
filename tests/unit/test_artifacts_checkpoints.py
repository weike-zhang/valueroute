from __future__ import annotations

import json
from pathlib import Path

import pytest

from valueroute.storage.artifacts import ArtifactError, ArtifactStore
from valueroute.storage.checkpoints import Checkpoint, CheckpointError, CheckpointStore


def checkpoint(reference=None) -> Checkpoint:
    return Checkpoint(
        id="cp_1",
        boundary_version=2,
        owner_version=3,
        confirmed_facts=["test suite passed before the checkpoint"],
        open_questions=["whether upstream service is available"],
        resources=[{"resource_id": "workspace:src/app.py", "revision": "abc123"}],
        leases=[{"lease_id": "lease_1", "status": "active"}],
        modification_summary="Added durable artifact persistence.",
        executed_commands=["pytest tests/unit/test_artifacts_checkpoints.py"],
        evidence_refs=["ev_1"],
        recent_failures=["previous upload verification failed"],
        next_step="resume from the safe boundary",
        usage={"input_tokens": 10, "output_tokens": 4, "cost_usd": 0.01, "wall_time_ms": 12},
        safe_to_resume=True,
        artifact_refs=[] if reference is None else [reference],
    )


def test_artifact_is_sha256_addressed_and_read_is_integrity_checked(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    reference = store.put(b"large body", media_type="text/plain", data_classification="confidential")

    assert reference.relative_path == f"artifacts/sha256/{reference.sha256}"
    assert store.get(reference) == b"large body"
    assert not list((tmp_path / "artifacts" / "sha256").glob("*.tmp"))

    (tmp_path / reference.relative_path).write_bytes(b"tampered")
    with pytest.raises(ArtifactError, match="artifact_integrity_mismatch"):
        store.get(reference)


def test_checkpoint_requires_existing_verified_artifacts_before_atomic_commit(tmp_path: Path):
    artifacts = ArtifactStore(tmp_path)
    checkpoints = CheckpointStore(tmp_path, artifacts)
    reference = artifacts.put(b"evidence output", media_type="text/plain")

    checkpoints.save(checkpoint(reference))
    saved = tmp_path / "checkpoints" / "cp_1.json"
    assert checkpoints.load("cp_1") == checkpoint(reference)
    assert not list(saved.parent.glob("*.tmp"))

    missing = reference.to_dict() | {"sha256": "0" * 64, "relative_path": "artifacts/sha256/" + "0" * 64}
    with pytest.raises(ArtifactError, match="artifact_missing"):
        checkpoints.save(checkpoint(missing))
    assert not (tmp_path / "checkpoints" / "cp_1.json.tmp").exists()


def test_checkpoint_and_referenced_artifact_are_verified_on_read(tmp_path: Path):
    artifacts = ArtifactStore(tmp_path)
    checkpoints = CheckpointStore(tmp_path, artifacts)
    reference = artifacts.put(b"artifact")
    checkpoints.save(checkpoint(reference))

    saved = tmp_path / "checkpoints" / "cp_1.json"
    envelope = json.loads(saved.read_text())
    envelope["checkpoint"]["next_step"] = "corrupted after commit"
    saved.write_text(json.dumps(envelope))
    with pytest.raises(CheckpointError, match="checkpoint_integrity_mismatch"):
        checkpoints.load("cp_1")

    checkpoints.save(checkpoint(reference))
    (tmp_path / reference.relative_path).write_bytes(b"changed")
    with pytest.raises(ArtifactError, match="artifact_integrity_mismatch"):
        checkpoints.load("cp_1")
