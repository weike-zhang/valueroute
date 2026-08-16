"""T1 read-only cross-provider handoff (design section 18.4, FR-301).

Handoff is self-contained, low-risk, and read-only by default: the target
provider receives the declared fields and never gains controller authority.
The handoff only happens when the egress policy allows the data
classification, destination provider, and field set; the resulting egress is
journaled in the ledger.
"""

from __future__ import annotations

from typing import Any

from valueroute.domain.errors import DomainError
from valueroute.domain.models import ControllerEpoch, ParentTask, WorkerAttempt, new_id
from valueroute.egress.ledger import EgressLedger
from valueroute.egress.models import EgressPolicy
from valueroute.storage.interfaces import StateStore


class HandoffService:
    """Coordinates a T1 read-only cross-provider handoff with egress audit."""

    def __init__(self, store: StateStore, *, ledger: EgressLedger | None = None, policy: EgressPolicy | None = None):
        self.store = store
        self.ledger = ledger or EgressLedger(store)
        self.policy = policy or EgressPolicy()

    def handoff_attempt(
        self,
        attempt_id: str,
        *,
        target_provider: str,
        target_model: str,
        fields: list[str],
        data_classification: str,
        reason: str = "read_only_handoff",
        idem: tuple[str, str, str] | None = None,
        payload: Any | None = None,
    ) -> dict[str, Any]:
        """Hand off a WorkerAttempt to another provider in read-only T1 mode.

        The attempt must already be claimed by a source provider; the handoff
        re-points the attempt's provider context and journals an egress record
        describing the data that left the source provider.  When ``idem`` is
        given, a replay with the same key and payload returns the original
        handoff result instead of handing off twice.
        """
        if idem is not None:
            previous = self.store.check_idempotency(idem, payload)
            if previous:
                return dict(previous["event"]["data"])
        attempt = self.store.attempts.get(attempt_id)
        if attempt is None:
            raise DomainError("not_found", "worker attempt not found", 404)
        if not self.policy.allows(data_classification, target_provider=target_provider, fields=fields):
            raise DomainError("egress_denied", "egress policy does not allow this cross-provider handoff")

        task = self._parent_task(attempt.child_task_id)
        source_provider = self._source_provider(task, attempt)

        result = {
            "attempt_id": attempt.id,
            "egress_id": new_id("egress"),
            "source_provider": source_provider,
            "target_provider": target_provider,
            "target_model": target_model,
            "data_classification": data_classification,
            "fields": fields,
            "reason": reason,
            "mode": "read_only_handoff",
        }
        egress = self.ledger.record(
            task_id=task.id if task is not None else None,
            child_task_id=attempt.child_task_id,
            source_provider=source_provider,
            target_provider=target_provider,
            data_classification=data_classification,
            fields=fields,
        )
        result["egress_id"] = egress.id

        updated = attempt.model_copy(
            update={
                "version": attempt.version + 1,
                "provider_id": target_provider,
                "model_id": target_model,
            }
        )
        self.store.attempts[attempt.id] = updated
        self.store.commit({"type": "worker.handed_off", "data": updated.model_dump(mode="json")})
        if idem is not None:
            self.store.commit({"type": "egress.handoff_result", "data": result}, key=idem, payload=payload or result)
        return result

    def _parent_task(self, child_task_id: str) -> ParentTask | None:
        return next(
            (task for task in self.store.tasks.values() if child_task_id in task.child_task_ids),
            None,
        )

    def _source_provider(self, task: ParentTask | None, attempt: WorkerAttempt) -> str:
        if task is None or task.controller_session_id is None:
            return "unknown"
        session = self.store.sessions.get(task.controller_session_id)
        if session is None or session.active_controller_epoch_id is None:
            return "unknown"
        epoch = self.store.epochs.get(session.active_controller_epoch_id)
        return epoch.provider_id if isinstance(epoch, ControllerEpoch) else "unknown"


__all__ = ["HandoffService"]
