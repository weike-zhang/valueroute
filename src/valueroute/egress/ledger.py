"""Durable egress ledger (design section 18.4, FR-302).

Records every cross-provider egress into the journal so replay restores a
complete, auditable trail of what left the trusted provider.
"""

from __future__ import annotations

from typing import Any

from valueroute.egress.models import EgressRecord, new_egress_record
from valueroute.storage.interfaces import StateStore


class EgressLedger:
    """Append-only egress audit trail backed by the journal."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def record(self, *, idem: tuple[str, str, str] | None = None, payload: Any | None = None, **data: Any) -> EgressRecord:
        record = new_egress_record(**data)
        self.store.egress_records[record.id] = record.model_dump(mode="json")
        self.store.commit({"type": "egress.recorded", "data": record.model_dump(mode="json")}, key=idem, payload=payload)
        return record

    def list(self, *, task_id: str | None = None, target_provider: str | None = None) -> list[EgressRecord]:
        records = [EgressRecord.model_validate(value) for value in self.store.egress_records.values()]
        if task_id is not None:
            records = [record for record in records if record.task_id == task_id]
        if target_provider is not None:
            records = [record for record in records if record.target_provider == target_provider]
        return sorted(records, key=lambda record: (record.recorded_at, record.id))


__all__ = ["EgressLedger"]
