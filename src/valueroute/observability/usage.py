from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Iterable

from pydantic import Field

from valueroute.domain.models import StrictModel, now


class CostStatus(str, Enum):
    known = "known"
    unknown = "unknown"


class UsageRecord(StrictModel):
    """One provider call's usage; unknown cost is represented explicitly."""

    id: str
    task_id: str
    provider_id: str
    model_id: str
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost_status: CostStatus = CostStatus.unknown
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    retries: int = Field(default=0, ge=0)
    recorded_at: datetime = Field(default_factory=now)

    def __init__(self, **data):
        super().__init__(**data)
        if self.cost_status is CostStatus.unknown and self.cost_usd is not None:
            raise ValueError("unknown cost must not include cost_usd")
        if self.cost_status is CostStatus.known and self.cost_usd is None:
            raise ValueError("known cost requires cost_usd")


class UsageTotals(StrictModel):
    """Task-level totals that preserve unknown cost instead of guessing zero."""

    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    cost_status: CostStatus = CostStatus.unknown
    cost_usd: float | None = Field(default=None, ge=0)

    def __init__(self, **data):
        super().__init__(**data)
        if self.cost_status is CostStatus.unknown and self.cost_usd is not None:
            raise ValueError("unknown cost must not include cost_usd")
        if self.cost_status is CostStatus.known and self.cost_usd is None:
            raise ValueError("known cost requires cost_usd")


class TaskUsageReport(StrictModel):
    """Stable read-only query/export payload for one task's provider calls."""

    task_id: str
    records: list[UsageRecord]
    totals: UsageTotals


def build_usage_report(task_id: str, records: Iterable[UsageRecord]) -> TaskUsageReport:
    """Build a deterministic task usage report from journal-backed records."""

    records = list(records)
    known_costs = [record.cost_usd for record in records if record.cost_status is CostStatus.known]
    has_unknown_cost = any(record.cost_status is CostStatus.unknown for record in records)
    if has_unknown_cost or len(known_costs) != len(records):
        cost_status = CostStatus.unknown
        cost_usd = None
    else:
        cost_status = CostStatus.known
        cost_usd = sum(cost for cost in known_costs if cost is not None)

    return TaskUsageReport(
        task_id=task_id,
        records=records,
        totals=UsageTotals(
            input_tokens=sum(record.input_tokens or 0 for record in records),
            cached_input_tokens=sum(record.cached_input_tokens or 0 for record in records),
            output_tokens=sum(record.output_tokens or 0 for record in records),
            reasoning_tokens=sum(record.reasoning_tokens or 0 for record in records),
            latency_ms=sum(record.latency_ms for record in records),
            retries=sum(record.retries for record in records),
            cost_status=cost_status,
            cost_usd=cost_usd,
        ),
    )


USAGE_EXPORT_FIELDS = (
    "id",
    "task_id",
    "provider_id",
    "model_id",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cost_status",
    "cost_usd",
    "latency_ms",
    "retries",
    "recorded_at",
)


def usage_export_rows(report: TaskUsageReport) -> list[dict[str, object]]:
    """Return CSV-ready rows without converting unknown cost to a number."""

    return [
        {field: record.model_dump(mode="json").get(field) for field in USAGE_EXPORT_FIELDS}
        for record in report.records
    ]
