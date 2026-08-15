from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from valueroute.settings import ensure_storage_capacity


class ArtifactError(RuntimeError):
    """Raised when content-addressed artifact storage cannot prove integrity."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """The journal-safe, relative reference to one immutable artifact."""

    relative_path: str
    media_type: str
    size: int
    sha256: str
    data_classification: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ArtifactRef":
        try:
            reference = cls(
                relative_path=str(value["relative_path"]),
                media_type=str(value["media_type"]),
                size=int(value["size"]),
                sha256=str(value["sha256"]),
                data_classification=str(value["data_classification"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ArtifactError("invalid_artifact_reference") from error
        if reference.size < 0 or len(reference.sha256) != 64:
            raise ArtifactError("invalid_artifact_reference")
        return reference


def _fsync_directory(path: Path) -> None:
    """Persist a rename's directory entry on platforms that support it."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ArtifactStore:
    """Filesystem CAS for large bodies referenced by journal/checkpoint data."""

    def __init__(self, root: Path, *, max_bytes: int | None = None, min_free_bytes: int = 0):
        self.root = Path(root)
        self.artifacts_root = self.root / "artifacts" / "sha256"
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes

    def put(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        data_classification: str = "internal",
    ) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if not media_type or not data_classification:
            raise ValueError("artifact metadata must be non-empty")
        ensure_storage_capacity(self.root, incoming_bytes=len(content), max_bytes=self.max_bytes, min_free_bytes=self.min_free_bytes)

        digest = hashlib.sha256(content).hexdigest()
        target = self.artifacts_root / digest
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self.verify(self._reference(target, digest, len(content), media_type, data_classification))
            return self._reference(target, digest, len(content), media_type, data_classification)

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=self.artifacts_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.replace(temporary, target)
            except FileExistsError:
                # A concurrent producer may have won the race. Its bytes still
                # need to verify before its reference can be returned.
                pass
            _fsync_directory(self.artifacts_root)
        finally:
            if temporary.exists():
                temporary.unlink()

        reference = self._reference(target, digest, len(content), media_type, data_classification)
        self.verify(reference)
        return reference

    def get(self, reference: ArtifactRef | dict[str, object]) -> bytes:
        reference = self.verify(reference)
        return self._path_for(reference).read_bytes()

    def verify(self, reference: ArtifactRef | dict[str, object]) -> ArtifactRef:
        reference = self._coerce_reference(reference)
        path = self._path_for(reference)
        if not path.is_file():
            raise ArtifactError(f"artifact_missing: {reference.relative_path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        if size != reference.size or digest.hexdigest() != reference.sha256:
            raise ArtifactError(f"artifact_integrity_mismatch: {reference.relative_path}")
        return reference

    def _reference(self, target: Path, digest: str, size: int, media_type: str, data_classification: str) -> ArtifactRef:
        return ArtifactRef(
            relative_path=target.relative_to(self.root).as_posix(),
            media_type=media_type,
            size=size,
            sha256=digest,
            data_classification=data_classification,
        )

    def _coerce_reference(self, reference: ArtifactRef | dict[str, object]) -> ArtifactRef:
        return reference if isinstance(reference, ArtifactRef) else ArtifactRef.from_dict(reference)

    def _path_for(self, reference: ArtifactRef) -> Path:
        expected = Path("artifacts") / "sha256" / reference.sha256
        if Path(reference.relative_path) != expected:
            raise ArtifactError("artifact_reference_path_mismatch")
        return self.root / expected


LocalArtifactStore = ArtifactStore
