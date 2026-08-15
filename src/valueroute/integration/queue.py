"""Small process-local IntegrationAttempt queue backed by the journal."""
from __future__ import annotations

from dataclasses import dataclass
from valueroute.domain.models import IntegrationAttempt, IntegrationAttemptStatus, now
from valueroute.storage.interfaces import StateStore

@dataclass(frozen=True)
class IntegrationClaim:
    attempt: IntegrationAttempt
    token: str

class IntegrationQueue:
    def __init__(self, store: StateStore):
        self.store = store
        self._claims: dict[str, str] = {}

    def enqueue(self, attempt: IntegrationAttempt) -> IntegrationAttempt:
        if attempt.status is not IntegrationAttemptStatus.queued:
            raise ValueError("integration_enqueue_requires_queued_attempt")
        return self.store.record_integration_attempt(attempt, "integration.queued")

    def claim(self, attempt_id: str | None = None, *, token: str = "local") -> IntegrationClaim | None:
        candidates = [a for a in self.store.integration_attempts.values()
                      if a.status is IntegrationAttemptStatus.queued
                      and a.id not in self._claims
                      and (attempt_id is None or a.id == attempt_id)]
        if not candidates:
            return None
        attempt = min(candidates, key=lambda a: (a.order_index, a.created_at, a.id))
        running = attempt.model_copy(update={"status": IntegrationAttemptStatus.running,
                                             "version": attempt.version + 1, "updated_at": now()})
        self.store.record_integration_attempt(running, "integration.started")
        self._claims[running.id] = token
        return IntegrationClaim(running, token)

    def ack(self, claim: IntegrationClaim, terminal: IntegrationAttempt) -> IntegrationAttempt:
        if self._claims.get(claim.attempt.id) != claim.token:
            raise ValueError("integration_claim_not_owned")
        events = {IntegrationAttemptStatus.integrated: "integration.completed",
                  IntegrationAttemptStatus.conflicted: "integration.conflict",
                  IntegrationAttemptStatus.rejected: "integration.blocked"}
        if terminal.id != claim.attempt.id or terminal.status not in events:
            raise ValueError("integration_ack_requires_terminal_attempt")
        result = self.store.record_integration_attempt(terminal, events[terminal.status])
        self._claims.pop(claim.attempt.id, None)
        return result

    def requeue(self, claim: IntegrationClaim, *, message: str | None = None) -> IntegrationAttempt:
        if self._claims.get(claim.attempt.id) != claim.token:
            raise ValueError("integration_claim_not_owned")
        queued = claim.attempt.model_copy(update={"status": IntegrationAttemptStatus.queued,
                                                  "version": claim.attempt.version + 1,
                                                  "message": message, "updated_at": now()})
        self.store.record_integration_attempt(queued, "integration.requeued")
        self._claims.pop(claim.attempt.id, None)
        return queued

    def recover(self) -> int:
        count = 0
        for attempt in list(self.store.integration_attempts.values()):
            if attempt.status is IntegrationAttemptStatus.running:
                queued = attempt.model_copy(update={"status": IntegrationAttemptStatus.queued,
                                                    "version": attempt.version + 1, "updated_at": now()})
                self.store.record_integration_attempt(queued, "integration.requeued")
                count += 1
        self._claims.clear()
        return count
