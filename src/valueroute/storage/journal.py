from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, ClassVar

from valueroute.settings import ensure_storage_capacity


class JournalError(RuntimeError):
    pass


class InstanceLockedError(JournalError):
    """Raised when another ValueRoute instance owns this data directory."""


class JournalRecoveryError(JournalError):
    """Raised for journal damage that cannot safely be treated as a tail."""


class SnapshotRecoveryError(JournalError):
    """Raised when a selected snapshot is structurally valid but unusable."""


class InstanceLock:
    """An advisory process lock, with an in-process guard for clear local errors."""

    _held_paths: ClassVar[set[Path]] = set()
    _held_paths_lock = Lock()

    def __init__(self, root: Path, *, max_bytes: int | None = None, min_free_bytes: int = 0):
        self.path = root / "instance.lock"
        self._file: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        resolved = self.path.resolve()
        with self._held_paths_lock:
            if resolved in self._held_paths:
                raise InstanceLockedError(f"instance_locked: {self.path.parent}")
            self._held_paths.add(resolved)
        try:
            self._file = self.path.open("a+", encoding="utf-8")
            try:
                if os.name == "nt":
                    import msvcrt

                    self._file.seek(0)
                    if not self._file.read(1):
                        self._file.write("\0")
                        self._file.flush()
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                raise InstanceLockedError(f"instance_locked: {self.path.parent}") from error
            self._file.seek(0)
            self._file.truncate()
            self._file.write(f"pid={os.getpid()}\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        if self._file is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None
        with self._held_paths_lock:
            self._held_paths.discard(self.path.resolve())


class LocalJournal:
    """Append-only, checksummed commit frames for the single-instance store."""

    format_version = 1
    snapshot_format_version = 1

    def __init__(self, root: Path, *, max_bytes: int | None = None, min_free_bytes: int = 0):
        self.root = root
        self.path = root / "journal" / "active.jsonl"
        self.lock = RLock()
        self.instance_lock = InstanceLock(root)
        self.sequence = 0
        self._snapshot_sequence = 0
        self._records: list[dict[str, Any]] = []
        self._idempotency: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
        self.recovery_diagnostics: list[dict[str, str]] = []
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.instance_lock.acquire()
        try:
            self._load()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.instance_lock.release()

    def __enter__(self) -> LocalJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _quarantine_tail(self, raw_frame: bytes, line_number: int, reason: str) -> None:
        quarantine = self.root / "quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = quarantine / f"journal-tail-{stamp}.jsonl"
        with target.open("xb") as output:
            output.write(raw_frame)
            output.flush()
            os.fsync(output.fileno())
        self.recovery_diagnostics.append({
            "code": "journal_tail_quarantined",
            "line": str(line_number),
            "reason": reason,
            "quarantine_path": str(target),
        })

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            try:
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                # Directory fsync is not available on every supported platform.
                # The file itself is still fully fsynced before replacement.
                pass
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _decode_snapshot(cls, raw: bytes) -> tuple[int, list[dict[str, Any]]]:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or decoded.get("format_version") != cls.snapshot_format_version:
            raise SnapshotRecoveryError("snapshot_unsupported_format")
        records = decoded.get("records")
        if not isinstance(records, list):
            raise SnapshotRecoveryError("snapshot_invalid_records")
        payload = cls._canonical_json(records)
        if decoded.get("payload_hash") != hashlib.sha256(payload).hexdigest():
            raise SnapshotRecoveryError("snapshot_checksum_mismatch")
        sequence = int(decoded.get("sequence", 0))
        if sequence < 0 or any(int(record.get("sequence", 0)) > sequence for record in records):
            raise SnapshotRecoveryError("snapshot_sequence_mismatch")
        return sequence, records

    def _load_snapshot(self) -> None:
        snapshots = self.root / "snapshots"
        manifest = snapshots / "manifest.json"
        candidates: list[Path] = []
        if manifest.exists():
            try:
                pointer = json.loads(manifest.read_bytes())
                target = pointer.get("snapshot") if isinstance(pointer, dict) else None
                if isinstance(target, str):
                    candidates.append(snapshots / target)
            except (OSError, json.JSONDecodeError):
                self.recovery_diagnostics.append({"code": "snapshot_manifest_invalid"})
        candidates.extend(sorted(snapshots.glob("snapshot-*.json"), reverse=True))
        seen: set[Path] = set()
        for raw_candidate in candidates:
            resolved = raw_candidate.resolve()
            if resolved in seen or not resolved.is_file() or resolved.parent != snapshots.resolve():
                continue
            seen.add(resolved)
            try:
                sequence, records = self._decode_snapshot(resolved.read_bytes())
            except (OSError, json.JSONDecodeError, SnapshotRecoveryError) as error:
                self.recovery_diagnostics.append({"code": "snapshot_ignored", "path": str(resolved), "reason": str(error)})
                continue
            self._records.extend(records)
            self.sequence = sequence
            self._snapshot_sequence = sequence
            for record in records:
                idem = record.get("idempotency")
                if idem:
                    self._idempotency[tuple(idem["key"])] = (idem["request_hash"], idem["response"])
            return

    @staticmethod
    def _frame_records(raw_frame: bytes) -> list[dict[str, Any]]:
        decoded = json.loads(raw_frame)
        # v0.0.1 previously emitted a JSON list per line. Keep those committed
        # frames replayable while all new frames use checksummed envelopes.
        if isinstance(decoded, list):
            return decoded
        if not isinstance(decoded, dict) or decoded.get("format_version") != LocalJournal.format_version:
            raise ValueError("unsupported_frame")
        records = decoded.get("records")
        if not isinstance(records, list):
            raise ValueError("invalid_records")
        payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
        if decoded.get("length") != len(payload):
            raise ValueError("length_mismatch")
        if decoded.get("checksum") != hashlib.sha256(payload).hexdigest():
            raise ValueError("checksum_mismatch")
        if decoded.get("sequence_start") != (records[0]["sequence"] if records else decoded.get("sequence_end", 0) + 1):
            raise ValueError("sequence_start_mismatch")
        if decoded.get("sequence_end") != (records[-1]["sequence"] if records else decoded.get("sequence_start", 1) - 1):
            raise ValueError("sequence_end_mismatch")
        if decoded.get("payload_hash") != hashlib.sha256(LocalJournal._canonical_json(records)).hexdigest():
            raise ValueError("payload_hash_mismatch")
        return records

    def _load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_snapshot()
        if not self.path.exists():
            return
        frames = self.path.read_bytes().splitlines(keepends=True)
        non_empty = [index for index, frame in enumerate(frames) if frame.strip()]
        final_index = non_empty[-1] if non_empty else -1
        valid_end = 0
        for index, raw_frame in enumerate(frames):
            if not raw_frame.strip():
                valid_end += len(raw_frame)
                continue
            try:
                records = self._frame_records(raw_frame)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
                if index == final_index:
                    self._quarantine_tail(raw_frame, index + 1, str(error))
                    with self.path.open("r+b") as output:
                        output.truncate(valid_end)
                        output.flush()
                        os.fsync(output.fileno())
                    return
                raise JournalRecoveryError(
                    f"journal_corrupt_non_tail: line={index + 1}; reason={error}; "
                    f"repair {self.path} before restart"
                ) from error
            for record in records:
                if int(record.get("sequence", 0)) <= self._snapshot_sequence:
                    continue
                self._records.append(record)
                self.sequence = max(self.sequence, int(record.get("sequence", 0)))
                idem = record.get("idempotency")
                if idem:
                    self._idempotency[tuple(idem["key"])] = (idem["request_hash"], idem["response"])
            valid_end += len(raw_frame)

    @staticmethod
    def request_hash(payload: Any) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    def idempotent_result(self, key: tuple[str, str, str], request_hash: str) -> dict[str, Any] | None:
        found = self._idempotency.get(key)
        if not found:
            return None
        old_hash, response = found
        if old_hash != request_hash:
            raise JournalError("idempotency_conflict")
        return response

    def append_frame(
        self,
        events: Sequence[dict[str, Any]],
        *,
        expected_versions: Mapping[str, int] | None = None,
        idempotency: tuple[tuple[str, str, str], str, dict[str, Any]] | None = None,
    ) -> None:
        with self.lock:
            events = list(events)
            sequence_start = self.sequence + 1
            records = []
            for event in events:
                self.sequence += 1
                records.append({"sequence": self.sequence, "event": event})
            if idempotency:
                key, request_hash, response = idempotency
                records.append({"sequence": self.sequence, "idempotency": {"key": list(key), "request_hash": request_hash, "response": response}})
            payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
            commit = {
                "format_version": self.format_version,
                "commit_id": f"cmt_{uuid.uuid4().hex}",
                "sequence_start": sequence_start,
                "sequence_end": self.sequence,
                "expected_versions": dict(expected_versions or {}),
                "payload_hash": hashlib.sha256(self._canonical_json(records)).hexdigest(),
                "committed_at": datetime.now(timezone.utc).isoformat(),
                "length": len(payload),
                "checksum": hashlib.sha256(payload).hexdigest(),
                "records": records,
            }
            frame = json.dumps(commit, ensure_ascii=False, separators=(",", ":")) + "\n"
            ensure_storage_capacity(self.root, incoming_bytes=len(frame.encode("utf-8")), max_bytes=self.max_bytes, min_free_bytes=self.min_free_bytes)
            with self.path.open("a", encoding="utf-8") as output:
                output.write(frame)
                output.flush()
                os.fsync(output.fileno())
            self._records.extend(records)
            if idempotency:
                key, request_hash, response = idempotency
                self._idempotency[key] = (request_hash, response)

    def append(self, events: list[dict[str, Any]], *, idempotency: tuple[tuple[str, str, str], str, dict[str, Any]] | None = None) -> None:
        """Compatibility wrapper; new callers should use append_frame."""
        self.append_frame(events, idempotency=idempotency)

    def snapshot(self, *, aggregate_versions: Mapping[str, int] | None = None) -> dict[str, Any]:
        """Durably write a replay snapshot without changing the active journal."""
        with self.lock:
            records = list(self._records)
            payload_hash = hashlib.sha256(self._canonical_json(records)).hexdigest()
            sequence = self.sequence
            name = f"snapshot-{sequence:020d}.json"
            envelope = {
                "format_version": self.snapshot_format_version,
                "sequence": sequence,
                "record_count": len(records),
                "aggregate_versions": dict(aggregate_versions or {}),
                "payload_hash": payload_hash,
                "records": records,
            }
            target = self.root / "snapshots" / name
            self._atomic_bytes(target, self._canonical_json(envelope))
            manifest = {"format_version": self.snapshot_format_version, "snapshot": name, "sequence": sequence, "payload_hash": payload_hash}
            self._atomic_bytes(self.root / "snapshots" / "manifest.json", self._canonical_json(manifest))
            return manifest

    def compact(self, *, aggregate_versions: Mapping[str, int] | None = None) -> dict[str, Any]:
        """Create a durable snapshot while retaining the journal as a recovery source.

        Physical journal truncation is deliberately a safe no-op for this local
        adapter: without immutable journal segments, deleting the active log
        would make a corrupted newest snapshot lossy. Snapshot generations are
        still useful for restart/replay and can be compacted safely once a
        segment-retention policy exists.
        """
        with self.lock:
            manifest = self.snapshot(aggregate_versions=aggregate_versions)
            manifest = manifest | {"compacted": False, "journal_retained": True}
            return manifest

    def events(self, after: int = 0) -> list[dict[str, Any]]:
        return [record["event"] | {"sequence": record["sequence"]} for record in self._records if "event" in record and record["sequence"] > after]
