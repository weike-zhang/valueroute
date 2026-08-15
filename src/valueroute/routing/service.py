from __future__ import annotations

from typing import Any

from valueroute.routing.advisory import AdvisoryEngine
from valueroute.routing.boundary import classify_boundary
from valueroute.routing.models import (
    RequestBoundaryDecision,
    RoutingAdvice,
    RoutingRequestEnvelope,
    ShadowRecord,
)
from valueroute.routing.profiler import Profiler
from valueroute.routing.shadow import ShadowLedger


class RoutingService:
    """Coordinates the read-only advisory routing pipeline.

    Pipeline: RoutingRequestEnvelope -> boundary decision -> RequirementGraph
    -> routing candidates -> (optional) durable shadow record.

    The service never registers a controller, never creates a WorkerPlan, and
    never changes model or policy configuration.  Shadow records are advisory
    only and grant no execution rights.
    """

    def __init__(
        self,
        store: object,
        *,
        profiler: Profiler | None = None,
        advisory: AdvisoryEngine | None = None,
        shadow: ShadowLedger | None = None,
    ) -> None:
        self.store = store
        self.profiler = profiler or Profiler()
        self.advisory = advisory or AdvisoryEngine()
        self.shadow = shadow or ShadowLedger(store)

    def analyze(self, envelope: RoutingRequestEnvelope) -> tuple[RoutingAdvice, RequestBoundaryDecision]:
        boundary = classify_boundary(envelope)
        graph = self.profiler.profile(envelope)
        advice = self.advisory.advise(envelope, boundary, graph)
        return advice, boundary

    def analyze_and_shadow(self, envelope: RoutingRequestEnvelope, envelope_json: dict[str, Any], *, key: tuple[str, str, str] | None = None) -> tuple[RoutingAdvice, ShadowRecord | None]:
        previous = self.store.check_idempotency(key, envelope_json)
        if previous:
            record = ShadowRecord.model_validate(previous["event"]["data"])
            return record.advice, record
        advice, _ = self.analyze(envelope)
        record = self.shadow.record(advice, envelope_json, key=key)
        return advice, record

    def list_shadow(self) -> list[ShadowRecord]:
        return self.shadow.list()


__all__ = ["RoutingService"]
