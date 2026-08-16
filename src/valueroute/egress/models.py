"""Cross-provider handoff and egress records (design section 18.4, FR-301/302).

T1 handoff is self-contained, low-risk, and read-only by default.  Every
cross-provider egress is journaled in an EgressLedger so that what left the
trusted provider (fields, data classification, destination) is auditable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from valueroute.domain.models import StrictModel, new_id, now

DataClassification = Literal["public", "internal", "confidential", "restricted"]

EgressMode = Literal["read_only_handoff"]

# The default T1 policy: egress is allowed only for self-contained low-risk
# read-only work, and restricted data never leaves the trusted provider unless
# an explicit policy allows it.
DEFAULT_ALLOWED_CLASSIFICATIONS: frozenset[str] = frozenset({"public", "internal"})


class EgressPolicy(StrictModel):
    """Field-and-classification-level policy for cross-provider egress.

    The Worker Provider authorization never automatically extends to the
    Controller Provider (design 17.3); the policy states explicitly which data
    classifications and field prefixes may leave the trusted provider and to
    which destination providers.
    """

    allowed_classifications: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_CLASSIFICATIONS))
    allowed_field_prefixes: list[str] = Field(default_factory=lambda: ["task_id", "child_task_id", "goal", "acceptance"])
    allowed_target_providers: list[str] = Field(default_factory=list, min_length=0)
    mode: EgressMode = "read_only_handoff"

    def allows(self, classification: str, *, target_provider: str, fields: list[str]) -> bool:
        if classification not in self.allowed_classifications:
            return False
        if self.allowed_target_providers and target_provider not in self.allowed_target_providers:
            return False
        if not fields:
            return True
        return all(
            any(
                field == prefix or field.startswith(prefix + "_")
                for prefix in self.allowed_field_prefixes
            )
            for field in fields
        )


class EgressRecord(StrictModel):
    """One journaled cross-provider egress event."""

    id: str
    task_id: str | None = None
    child_task_id: str | None = None
    source_provider: str
    target_provider: str
    data_classification: str
    fields: list[str] = Field(default_factory=list, max_length=200)
    mode: EgressMode = "read_only_handoff"
    recorded_at: datetime = Field(default_factory=now)


def new_egress_record(**data) -> EgressRecord:
    data.setdefault("id", new_id("egress"))
    return EgressRecord.model_validate(data)


__all__ = ["DEFAULT_ALLOWED_CLASSIFICATIONS", "EgressPolicy", "EgressRecord", "new_egress_record"]
