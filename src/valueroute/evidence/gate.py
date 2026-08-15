from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from valueroute.domain.models import Acceptance, EvidenceRecord, ObservationStatus


@dataclass(frozen=True)
class EvidenceGateResult:
    """The observable facts used to decide whether completion is honest."""

    passed: bool
    missing_required: tuple[str, ...] = ()
    failed_required: tuple[str, ...] = ()
    unobserved_required: tuple[str, ...] = ()

    @property
    def can_complete(self) -> bool:
        return self.passed


class EvidenceGate:
    """Validate the latest evidence for every required acceptance item."""

    def evaluate(
        self,
        acceptance_contract: Iterable[Acceptance],
        evidence: Iterable[EvidenceRecord],
    ) -> EvidenceGateResult:
        latest: dict[str, EvidenceRecord] = {}
        for record in evidence:
            previous = latest.get(record.requirement_id)
            if previous is None or record.recorded_at >= previous.recorded_at:
                latest[record.requirement_id] = record

        missing: list[str] = []
        failed: list[str] = []
        unobserved: list[str] = []
        for acceptance in acceptance_contract:
            if not acceptance.required:
                continue
            record = latest.get(acceptance.id)
            if record is None:
                missing.append(acceptance.id)
            elif record.observation_status is ObservationStatus.unobserved:
                unobserved.append(acceptance.id)
            elif record.observation_status is ObservationStatus.observed_fail:
                failed.append(acceptance.id)
            elif record.observation_status not in {ObservationStatus.observed_pass, ObservationStatus.not_applicable}:
                missing.append(acceptance.id)

        return EvidenceGateResult(
            passed=not (missing or failed or unobserved),
            missing_required=tuple(missing),
            failed_required=tuple(failed),
            unobserved_required=tuple(unobserved),
        )


def check_evidence_gate(
    acceptance_contract: Iterable[Acceptance], evidence: Iterable[EvidenceRecord]
) -> EvidenceGateResult:
    return EvidenceGate().evaluate(acceptance_contract, evidence)
