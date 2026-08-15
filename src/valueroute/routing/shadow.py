from __future__ import annotations

import hashlib
import json

from valueroute.domain.models import new_id
from valueroute.routing.models import RoutingAdvice, ShadowRecord


def envelope_hash(envelope: dict[str, object]) -> str:
    canonical = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ShadowLedger:
    """Durable, offline-only record of advice that was never executed.

    Shadow records are written to the journal with a request fingerprint so a
    later evaluation can compare them against real v0.0.1 results.  Writing a
    shadow record never grants execution rights.
    """

    def __init__(self, store: object) -> None:
        self.store = store
        self._records: dict[str, ShadowRecord] = getattr(store, "shadow_records", {})

    def record(self, advice: RoutingAdvice, envelope_json: dict[str, object], *, key: tuple[str, str, str] | None = None) -> ShadowRecord:
        record = ShadowRecord(
            id=new_id("shadow"),
            envelope_hash=envelope_hash(envelope_json),
            advice=advice,
        )
        self._records[record.id] = record
        payload = record.model_dump(mode="json")
        self.store.commit({"type": "routing.shadow_recorded", "data": payload}, key=key, payload=envelope_json)
        return record

    def mark_compared(self, record_id: str, outcome_ref: str) -> ShadowRecord:
        record = self._records[record_id]
        updated = record.model_copy(update={"status": "compared", "real_outcome_ref": outcome_ref})
        self._records[record_id] = updated
        self.store.commit(
            {
                "type": "routing.shadow_compared",
                "data": updated.model_dump(mode="json") | {"record_id": record_id, "outcome_ref": outcome_ref},
            }
        )
        return updated

    def list(self) -> list[ShadowRecord]:
        return list(self._records.values())


__all__ = ["ShadowLedger", "envelope_hash"]
