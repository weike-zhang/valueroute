import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class RuntimeProtectionError(RuntimeError):
    """Raised when a technical storage or execution safety limit is reached."""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeProtectionConfig:
    """Technical safety limits; these are not user task budgets."""

    provider_timeout_seconds: float = 60.0
    provider_retry_limit: int = 3
    claim_ttl_seconds: int = 60
    cancel_grace_seconds: int = 10
    max_artifact_bytes: int = 64 * 1024 * 1024
    max_checkpoint_bytes: int = 4 * 1024 * 1024
    max_journal_bytes: int = 256 * 1024 * 1024
    min_free_disk_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_environment(cls) -> "RuntimeProtectionConfig":
        timeout = float(os.getenv("VALUEROUTE_PROVIDER_TIMEOUT_SECONDS", "60"))
        if timeout <= 0:
            raise ValueError("VALUEROUTE_PROVIDER_TIMEOUT_SECONDS must be positive")
        return cls(
            provider_timeout_seconds=timeout,
            provider_retry_limit=_positive_int("VALUEROUTE_PROVIDER_RETRY_LIMIT", 3),
            claim_ttl_seconds=_positive_int("VALUEROUTE_CLAIM_TTL_SECONDS", 60),
            cancel_grace_seconds=_positive_int("VALUEROUTE_CANCEL_GRACE_SECONDS", 10),
            max_artifact_bytes=_positive_int("VALUEROUTE_MAX_ARTIFACT_BYTES", 64 * 1024 * 1024),
            max_checkpoint_bytes=_positive_int("VALUEROUTE_MAX_CHECKPOINT_BYTES", 4 * 1024 * 1024),
            max_journal_bytes=_positive_int("VALUEROUTE_MAX_JOURNAL_BYTES", 256 * 1024 * 1024),
            min_free_disk_bytes=_positive_int("VALUEROUTE_MIN_FREE_DISK_BYTES", 100 * 1024 * 1024),
        )


def ensure_storage_capacity(root: Path, *, incoming_bytes: int, max_bytes: int | None, min_free_bytes: int) -> None:
    if incoming_bytes < 0:
        raise ValueError("incoming_bytes must not be negative")
    if max_bytes is not None and root.exists():
        used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        if used + incoming_bytes > max_bytes:
            raise RuntimeProtectionError("storage_limit_exceeded")
    usage = shutil.disk_usage(root if root.exists() else root.parent)
    if usage.free - incoming_bytes < min_free_bytes:
        raise RuntimeProtectionError("disk_free_space_below_threshold")


def data_dir() -> Path:
    configured = os.getenv("VALUEROUTE_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "valueroute").resolve()
