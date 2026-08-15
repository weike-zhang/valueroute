# Changelog

All notable changes to ValueRoute are documented here. The format follows
Keep a Changelog; versions follow semantic versioning while 0.0.x is pre-1.0.

## [Unreleased]

### Added

- Close design section 20.1 test-coverage gaps:
  - disk-free-threshold guard in `ensure_storage_capacity`
    (`test_runtime_protection.py`);
  - terminal states stay immutable after journal replay (NFR-010,
    `test_state_machine.py`);
  - integration failure keeps the canonical workspace at the last committed
    revision (NFR-015, `test_integration_service.py`);
  - provider timeout racing cancel/pause never claims success
    (`test_runner.py`).
- Offline evaluation set (P1-7):
  - `evaluation/frozen_tasks.json` freezes three task families
    (backend/API diagnosis, frontend browser verification, disjoint
    full-stack) with five tasks each, ground-truth delegation, and
    acceptance criteria.
  - `scripts/evaluate_offline.py` runs advisory routing decisions and live
    A/B/C (zero worker / fixed one / adaptive) measurements against an
    OpenAI-compatible endpoint, recording tokens, wall time, and estimated
    cost with `quality_claim: false`.
  - First live evidence archived at
    `evaluation/evidence/evaluation-2026-08-15-gpt-5-6-mini.json`;
    design §23 acceptance checklist items 35/36 are now verified.

## [0.0.2] - 2026-08-15

### Added

- Read-only advisory routing pipeline (`src/valueroute/routing/`):
  - `RequestBoundaryDecision` classification via `classify_boundary`; host
    declarations win at full confidence, otherwise conservative keyword
    scoring, with an unparseable-input fallback the host can override.
  - `Profiler` turning a `RoutingRequestEnvelope` into a read-only
    `RequirementGraph` of requirements, constraints, and evidence gaps;
    `has_write_suggestion` is structurally always `False`.
  - `AdvisoryEngine` producing direct and worker candidates with rejection
    codes, estimated input/output tokens, cost, latency, confidence, and a
    stable basis version; control, clarification, and material-amendment
    requests fail closed.
  - `ShadowLedger` durable shadow records with a stable request fingerprint,
    `mark_compared` outcome attachment, and journal replay across restart.
- `OrchestrationMode.advisory` and the `RoutingService` pipeline.
- API surface `POST /v1/advisory`, `GET /v1/advisory/shadow`, and
  `GET /v1/advisory/shadow/{record_id}`; shadow recording is protected by
  `Idempotency-Key`.
- v1 request schema artifact `advisory-request.json` and updated request and
  response manifests.

### Changed

- `src/valueroute/domain/models.py`: `OrchestrationMode` now includes
  `advisory`.
- `src/valueroute/storage/store.py`: replay handles `routing.shadow_recorded`
  and `routing.shadow_compared` events into `Store.shadow_records`.
- Project version advanced to `0.0.2`.

### Security

- No known changes to the security boundary. Advisory mode is read-only and
  never modifies Controller, WorkerPlan, or model configuration.

## [0.0.1] - 2026-08-15

### Added

- FastAPI service with `/v1/health/live`, `/v1/health/ready`, session/task
  APIs, and a journal-backed SSE event endpoint.
- Local append-only JSONL journal with checksummed frames, tail quarantine,
  snapshot/compact, and non-tail corruption refusal.
- `off` and `worker_only` orchestration modes.
- Parent/Child task boundaries, deterministic `WorkerPlan` validation,
  expected-version and Idempotency-Key protection.
- ResourceRegion overlap checking and region-based WriterLease.
- Isolated local and Git workspaces, ChangeSet validation, ordered atomic
  integration, and ParentVerification.
- OpenAI Provider Adapter and AgentScope Framework Adapter lifecycle/SSE
  bridge.
- 0-5 Worker queue with claim, heartbeat, event-driven Checkpoint, kill-9
  recovery, and bounded `ExecutionSupervisor`.
- Evidence Gate, OwnerSelfReview/Verifier, Usage records, approvals with
  monotonic versioning, and runtime protections.
- Versioned v1 request/response JSON Schema artifacts checked against
  Pydantic and OpenAPI.

[0.0.2]: https://github.com/weike-zhang/valueroute/releases/tag/v0.0.2
[0.0.1]: https://github.com/weike-zhang/valueroute/releases/tag/v0.0.1