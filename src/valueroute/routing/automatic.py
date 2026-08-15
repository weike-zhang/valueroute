from __future__ import annotations

from valueroute.domain.errors import DomainError
from valueroute.domain.models import ControllerEpoch, OrchestrationMode, TaskStatus, new_id, now
from valueroute.routing.manifest import ModelProfile
from valueroute.routing.rank import ControllerRanker
from valueroute.storage.interfaces import StateStore

_RUNNING_TASK_STATUSES = {
    TaskStatus.queued,
    TaskStatus.running,
    TaskStatus.pause_requested,
    TaskStatus.cancel_requested,
    TaskStatus.waiting_approval,
}


class AutomaticControllerService:
    """FR-201/202: automatic controller selection with sticky epoch and safe switch.

    In ``automatic`` orchestration mode a controller is chosen once from the
    certified controller candidates and stays sticky.  A switch is only allowed
    across a safe boundary: no running task work may be in flight, the caller
    must confirm with the expected version, and the new epoch is committed
    atomically with the session update so a crash cannot leave a half-applied
    switch.
    """

    def __init__(self, store: StateStore, *, profiles: list[ModelProfile] | None = None, ranker: ControllerRanker | None = None):
        self.store = store
        self.profiles = profiles or []
        self.ranker = ranker or ControllerRanker()

    def _require_version(self, actual: int, expected: int) -> None:
        try:
            self.store.require_version(actual, expected)
        except ValueError as error:
            raise DomainError("version_conflict", str(error).replace("_", " ")) from error

    def ensure_controller(
        self,
        session_id: str,
        *,
        expected_version: int,
        reasoning_effort: str = "medium",
        idem: tuple[str, str, str] | None = None,
    ) -> ControllerEpoch:
        """Return the sticky controller epoch, selecting one on first use.

        Raises when the session is not in ``automatic`` mode, when a previously
        selected epoch is stale (caller must switch), or when no certified
        controller is available.
        """
        session = self.store.sessions.get(session_id)
        if not session:
            raise DomainError("not_found", "controller session not found", 404)
        self._require_version(session.version, expected_version)
        if session.orchestration_mode is not OrchestrationMode.automatic:
            raise DomainError("not_automatic_mode", "automatic controller selection requires automatic orchestration mode", 422)

        if session.active_controller_epoch_id:
            epoch = self.store.epochs.get(session.active_controller_epoch_id)
            if epoch is None:
                raise DomainError("epoch_not_found", "active controller epoch is missing", 500)
            return epoch  # sticky: no reselection while an epoch is active

        rank = self.ranker.select(self.profiles)
        if rank is None:
            raise DomainError("no_certified_controller", "no certified controller candidate is available")

        epoch = ControllerEpoch(
            id=new_id("ce"),
            controller_session_id=session_id,
            provider_id=rank.profile.provider_id,
            model_id=rank.profile.model_id,
            reasoning_effort=reasoning_effort,
            activated_at=now(),
        )
        self.store.epochs[epoch.id] = epoch
        updated = session.model_copy(update={"active_controller_epoch_id": epoch.id, "version": session.version + 1})
        self.store.sessions[session.id] = updated
        self.store.commit(
            {"type": "session.epoch_registered", "data": epoch.model_dump(mode="json")},
            key=idem,
            payload={"session_id": session_id, "expected_version": expected_version, "mode": "automatic"},
        )
        return epoch

    def switch_controller(
        self,
        session_id: str,
        *,
        expected_version: int,
        reasoning_effort: str = "medium",
        idem: tuple[str, str, str] | None = None,
    ) -> ControllerEpoch:
        """Switch the sticky controller across a safe boundary.

        Preconditions (FR-202): the session is in ``automatic`` mode, no task
        in the session is running or waiting, and the caller passes the current
        expected version.  The switch writes the new epoch and the updated
        session in one commit frame, so a crash cannot leave the session
        pointing at a stale epoch or a half-applied state.
        """
        if idem is not None:
            previous = self.store.check_idempotency(idem, {"session_id": session_id, "expected_version": expected_version, "mode": "automatic_switch"})
            if previous:
                return ControllerEpoch.model_validate(previous["event"]["data"])
        session = self.store.sessions.get(session_id)
        if not session:
            raise DomainError("not_found", "controller session not found", 404)
        self._require_version(session.version, expected_version)
        if session.orchestration_mode is not OrchestrationMode.automatic:
            raise DomainError("not_automatic_mode", "controller switching requires automatic orchestration mode", 422)

        running = [
            task.id
            for task in self.store.tasks.values()
            if task.controller_session_id == session_id and task.status in _RUNNING_TASK_STATUSES
        ]
        if running:
            raise DomainError("session_busy", "cannot switch controller while tasks are running in the session")

        rank = self.ranker.select(self.profiles)
        if rank is None:
            raise DomainError("no_certified_controller", "no certified controller candidate is available")

        current = self.store.epochs.get(session.active_controller_epoch_id) if session.active_controller_epoch_id else None
        next_version = (current.version + 1) if current is not None else 1
        epoch = ControllerEpoch(
            id=new_id("ce"),
            version=next_version,
            controller_session_id=session_id,
            provider_id=rank.profile.provider_id,
            model_id=rank.profile.model_id,
            reasoning_effort=reasoning_effort,
            activated_at=now(),
        )
        if current is not None:
            current = current.model_copy(update={"status": "released"})
            self.store.epochs[current.id] = current
            self.store.commit({"type": "controller_epoch.released", "data": current.model_dump(mode="json")})
        self.store.epochs[epoch.id] = epoch
        updated = session.model_copy(update={"active_controller_epoch_id": epoch.id, "version": session.version + 1})
        self.store.sessions[session.id] = updated
        self.store.commit(
            {"type": "session.epoch_registered", "data": epoch.model_dump(mode="json")},
            key=idem,
            payload={"session_id": session_id, "expected_version": expected_version, "mode": "automatic_switch"},
        )
        return epoch


__all__ = ["AutomaticControllerService"]
