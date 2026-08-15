"""Deterministic, journal-friendly ChangeSet integration orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from valueroute.domain.models import IntegrationAttempt, IntegrationAttemptStatus, WriterLease, new_id, now
from valueroute.storage.interfaces import StateStore
from valueroute.workspaces.interfaces import WorkspaceAdapter
from valueroute.workspaces.local import ChangeSet, IntegrationConflict
from valueroute.ownership.resolver import resolve_changeset
from valueroute.integration.queue import IntegrationClaim, IntegrationQueue


class IntegrationService:
    """Consume a plan's integration order without mutating the canonical tree directly."""

    def __init__(
        self,
        adapter: WorkspaceAdapter,
        append_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        store: StateStore | None = None,
        queue: IntegrationQueue | None = None,
    ):
        self.adapter = adapter
        self.append_event = append_event
        self.store = store
        self.queue = queue
        self._claims: dict[str, IntegrationClaim] = {}

    def integrate_in_order(
        self,
        integration_order: Iterable[str],
        changesets: Mapping[str, ChangeSet],
        leases: Iterable[WriterLease],
        *,
        parent_task_id: str | None = None,
        recover: bool = True,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        ordered_refs = list(integration_order)
        durable_leases = list(leases)
        for order_index, client_ref in enumerate(ordered_refs):
            previous = self.store.latest_integration_attempt(client_ref, parent_task_id) if self.store and recover else None
            if previous is not None:
                if previous.status is IntegrationAttemptStatus.integrated:
                    results.append(self.store.integration_result(previous))
                    continue
                if previous.status in {IntegrationAttemptStatus.conflicted, IntegrationAttemptStatus.rejected}:
                    results.append(self.store.integration_result(previous))
                    break
                result = {"client_ref": client_ref, "status": "blocked", "code": "integration_incomplete"}
                results.append(result)
                break

            attempt = self._new_attempt(client_ref, order_index, parent_task_id, changesets.get(client_ref))
            self._persist_attempt(attempt, "integration.queued")
            attempt = attempt.model_copy(update={"status": IntegrationAttemptStatus.running, "version": attempt.version + 1, "updated_at": now()})
            self._persist_attempt(attempt, "integration.started")

            changeset = changesets.get(client_ref)
            if changeset is None:
                result = {"client_ref": client_ref, "status": "blocked", "code": "changeset_missing"}
                attempt = attempt.model_copy(update={"status": IntegrationAttemptStatus.rejected, "version": attempt.version + 1, "code": "changeset_missing", "updated_at": now()})
                self._persist_terminal(attempt, "integration.blocked", result)
                results.append(result)
                break
            try:
                # The observed diff must be deterministically re-mapped before
                # the filesystem adapter is allowed to integrate it.
                resolve_changeset(changeset, durable_leases)
                snapshot = self.adapter.integrate(changeset, durable_leases)
            except IntegrationConflict as error:
                result = {"client_ref": client_ref, "status": "blocked", "code": "integration_conflict", "message": str(error), "owner_id": changeset.owner_id}
                attempt = attempt.model_copy(update={"status": IntegrationAttemptStatus.conflicted, "version": attempt.version + 1, "code": "integration_conflict", "message": str(error), "owner_id": changeset.owner_id, "updated_at": now()})
                self._persist_terminal(attempt, "integration.conflict", result)
                results.append(result)
                break
            except Exception as error:
                result = {"client_ref": client_ref, "status": "blocked", "code": "integration_rejected", "message": str(error), "owner_id": changeset.owner_id}
                attempt = attempt.model_copy(update={"status": IntegrationAttemptStatus.rejected, "version": attempt.version + 1, "code": "integration_rejected", "message": str(error), "owner_id": changeset.owner_id, "updated_at": now()})
                self._persist_terminal(attempt, "integration.blocked", result)
                results.append(result)
                break
            result = {"client_ref": client_ref, "status": "integrated", "owner_id": changeset.owner_id, "revision": snapshot.revision}
            attempt = attempt.model_copy(update={"status": IntegrationAttemptStatus.integrated, "version": attempt.version + 1, "revision": snapshot.revision, "owner_id": changeset.owner_id, "updated_at": now()})
            self._persist_terminal(attempt, "integration.completed", result)
            results.append(result)
        return results

    @staticmethod
    def _new_attempt(client_ref: str, order_index: int, parent_task_id: str | None, changeset: ChangeSet | None) -> IntegrationAttempt:
        return IntegrationAttempt(
            id=new_id("ia"),
            parent_task_id=parent_task_id,
            client_ref=client_ref,
            order_index=order_index,
            owner_id=changeset.owner_id if changeset is not None else None,
            base_revision=changeset.base_revision if changeset is not None else None,
        )

    def _persist_attempt(self, attempt: IntegrationAttempt, event_type: str) -> None:
        if self.queue is not None and event_type == "integration.queued":
            self.queue.enqueue(attempt)
            return
        if self.queue is not None and event_type == "integration.started":
            claim = self.queue.claim(attempt.id)
            if claim is None:
                raise RuntimeError("integration_claim_unavailable")
            self._claims[attempt.id] = claim
            return
        if self.store is not None:
            self.store.record_integration_attempt(attempt, event_type)

    def _persist_terminal(self, attempt: IntegrationAttempt, event_type: str, result: dict[str, Any]) -> None:
        claim = self._claims.pop(attempt.id, None)
        if self.queue is not None and claim is not None:
            self.queue.ack(claim, attempt)
        else:
            self._persist_attempt(attempt, event_type)
        self._record(event_type, result)

    def _record(self, event_type: str, data: dict[str, Any]) -> None:
        if self.append_event is not None:
            self.append_event({"type": event_type, "data": data})
