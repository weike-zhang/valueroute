"""Runtime admission rules for the Worker boundary."""

from __future__ import annotations

from dataclasses import dataclass

from valueroute.domain.errors import DomainError


MAX_WORKERS = 5
MAX_DELEGATION_DEPTH = 1


@dataclass(frozen=True)
class WorkerAdmission:
    """Validate one attempt to materialize a WorkerPlan at runtime."""

    actor_role: str = "controller"
    parent_depth: int = 0
    requested_workers: int = 0

    def validate(self) -> None:
        if self.actor_role not in {"controller", "worker"}:
            raise DomainError("invalid_actor_role", "worker admission actor role is invalid")
        if self.parent_depth < 0:
            raise DomainError("invalid_delegation_depth", "delegation depth cannot be negative")
        if self.requested_workers < 0 or self.requested_workers > MAX_WORKERS:
            raise DomainError("worker_limit_exceeded", "a parent task can have at most 5 workers")
        if self.parent_depth >= MAX_DELEGATION_DEPTH:
            raise DomainError("worker_depth_exceeded", "workers cannot create workers")
        if self.actor_role == "worker":
            raise DomainError("worker_spawn_forbidden", "a Worker cannot create another Worker")
