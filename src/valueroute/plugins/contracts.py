"""Public plugin contracts (design section 18.3, FR-204).

These are stable extension points for third-party implementations.  Each
contract is a structural Protocol so any object that implements the shape can
register; the runtime does not import or trust third-party code.  Registration
validates the declared role against the contract before the plugin is usable.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from valueroute.domain.models import VerificationRecord
from valueroute.routing.manifest import ModelProfile
from valueroute.routing.models import (
    RequestBoundaryDecision,
    RequirementGraph,
    RoutingAdvice,
    RoutingRequestEnvelope,
)

CONTRACT_VERSION = "0.1.0"

# Stable role identifiers exposed to third parties.
PLUGIN_ROLES = frozenset({
    "profiler",
    "controller_selector",
    "worker_policy",
    "provider",
    "framework",
    "verifier",
})


@runtime_checkable
class Profiler(Protocol):
    """Turns a RoutingRequestEnvelope into a read-only RequirementGraph.

    Must not execute anything, inspect controller state, or derive write
    permissions.  ``has_write_suggestion`` on the returned graph must stay
    ``False``.
    """

    def profile(self, envelope: RoutingRequestEnvelope) -> RequirementGraph: ...


@runtime_checkable
class ControllerSelector(Protocol):
    """Role-specific controller ranking for ``automatic`` mode.

    Must consider only certified, compatible controller candidates and never a
    single aggregate ranking shared with the Worker role.
    """

    def select(self, profiles: list[ModelProfile]) -> Any | None: ...


@runtime_checkable
class WorkerPolicy(Protocol):
    """Decides direct-vs-workers candidates for a request boundary.

    Returns RoutingAdvice with candidates, rejection reasons, and estimates.
    """

    def decide(
        self,
        envelope: RoutingRequestEnvelope,
        boundary: RequestBoundaryDecision,
        graph: RequirementGraph,
    ) -> RoutingAdvice: ...


@runtime_checkable
class Provider(Protocol):
    """Executes one model call for a WorkerAttempt.

    Must return an object exposing ``text``, ``usage``, and ``raw`` and raise
    ``ProviderCallError`` with a ``retryable`` flag on failure.
    """

    async def complete(
        self,
        *,
        task_id: str,
        input_text: str,
        reasoning_effort: str = "medium",
        retries: int = 0,
    ) -> Any: ...


@runtime_checkable
class Framework(Protocol):
    """Bridges a host framework (for example AgentScope) to ValueRoute.

    Implementations wrap the host's session/task lifecycle and translate
    events back to host format.
    """

    async def start_task(self, api: Any, host: dict[str, Any]) -> dict[str, Any]: ...

    async def control(self, api: Any, task_id: str, action: str, expected_version: int, *, idempotency_key: str) -> dict[str, Any]: ...

    async def resume_events(self, api: Any, task_id: str, *, last_event_id: str | None = None) -> list[dict[str, Any]]: ...


@runtime_checkable
class Verifier(Protocol):
    """Verifies persisted, scoped, observed evidence for the assigned Owner."""

    def verify(
        self,
        child_task_id: str,
        review_id: str,
        verifier_agent_id: str,
        evidence_ids: list[str] | tuple[str, ...],
        *,
        expected_review_version: int,
        idem: tuple[str, str, str] | None = None,
        payload: Any | None = None,
    ) -> VerificationRecord: ...


__all__ = [
    "CONTRACT_VERSION",
    "PLUGIN_ROLES",
    "ControllerSelector",
    "Framework",
    "Profiler",
    "Provider",
    "Verifier",
    "WorkerPolicy",
]
