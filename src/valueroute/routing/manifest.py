from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from valueroute.domain.models import StrictModel


class ModelProfile(StrictModel):
    """Versioned, role-specific model profile (design section 15.1).

    Worker and Controller roles are certified independently.  A single
    aggregate ranking must never replace role-specific status.
    """

    provider_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    measured_at: str = Field(min_length=1, max_length=200)
    protocol_status: Literal["compatible", "incompatible"]
    worker_status: Literal["candidate", "certified", "suspended"]
    controller_status: Literal["candidate", "certified", "suspended"]
    supported_modalities: list[str] = Field(default_factory=list, max_length=200)
    supported_tools: list[str] = Field(default_factory=list, max_length=200)
    effort_mapping: dict[str, str] = Field(default_factory=dict)
    region: str = Field(min_length=1, max_length=200)
    evidence_refs: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("provider_id")
    @classmethod
    def provider_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider_id must not be blank")
        return value

    @field_validator("model_id")
    @classmethod
    def model_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_id must not be blank")
        return value

    def eligible_for_controller(self) -> bool:
        return self.protocol_status == "compatible" and self.controller_status == "certified"

    def eligible_for_worker(self) -> bool:
        return self.protocol_status == "compatible" and self.worker_status == "certified"


def load_model_manifests(directory: Path | str) -> list[ModelProfile]:
    """Load every ``*.json`` model-manifest in a directory.

    Manifests that fail schema or value validation are skipped with a warning
    marker in the returned metadata; callers decide whether that fails closed.
    """
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"model manifest directory not found: {root}")
    profiles: list[ModelProfile] = []
    problems: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        if path.name == "model-manifest.schema.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            payload = data.get("model_profile", data)
            profiles.append(ModelProfile.model_validate(payload))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            problems.append({"path": str(path), "reason": str(error)})
    return profiles


__all__ = ["ModelProfile", "load_model_manifests"]
