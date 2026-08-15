# v0.0.1 acceptance matrix

This matrix records current evidence honestly; `partial` means the code has a useful boundary but the design's full acceptance evidence is still missing.

| ID | Current status | Evidence / remaining gap |
|---|---|---|
| FR-001 | pass | FastAPI, OpenAPI, live/ready, provider-injected Supervisor lifecycle, and checked-in v1 request/response publication manifests are present; a real Python 3.11/Uvicorn smoke served `/openapi.json`, live and ready, and a second process on the same data directory exited on the journal lock. |
| FR-002 | pass | Generationed checksummed replay snapshots, restart/fallback tests, ArtifactStore and CheckpointStore recovery exist; `compact()` safely retains the journal as source of truth. |
| FR-003 | pass | ControllerEpoch uniqueness and version checks are tested. |
| FR-004 | pass | WorkerPlan validation and invalid-plan contract tests. |
| FR-005 | pass | Persistent OwnerAssignment and single-active-owner tests. |
| FR-006 | pass | OwnerReview/Verifier models, region checks, fail-closed evidence validation, persistence replay and API contract tests are present. |
| FR-007 | pass | File/directory resolution is deterministic; database/external regions use the versioned schema plus an explicit registry-backed `SemanticRegionResolver` contract, with unsupported, failing, duplicate, and restart fail-closed tests. Production connectors are a later adapter boundary, not a v0.0.1 dependency. |
| FR-008 | pass | Overlap tests cover symbols, paths, row keys, ranges, partitions and subresources. |
| FR-009 | pass | Five-worker capacity guards and tests. |
| FR-010 | pass | Execution admission rejects Worker actors and delegation depth >= 1 with stable `worker_spawn_forbidden` / `worker_depth_exceeded` errors; controller admission remains capped at 5. |
| FR-011 | pass | `off` and `worker_only` API paths tested. |
| FR-012 | pass | Journal-backed queue plus bounded local `ExecutionSupervisor` can claim and drain 0–5 attempts; the HTTP service owns the supervisor lifecycle when a Provider adapter is configured, and contract tests cover the zero-worker and multi-worker paths. |
| FR-013 | pass | Checkpoint, SIGKILL-equivalent recovery, durable provider request context, supervisor restart consumption and a subprocess kill-9 continuation test are present. |
| FR-014 | pass | Write APIs use Idempotency-Key and replay tests. |
| FR-015 | pass | Pause/resume/cancel coordination exists; the OpenAI adapter now implements `cancel()` by aborting the in-flight request and returning confirmation, with fail-closed behavior when no request is running or stop is not confirmed. Runner-level cancellation and adapter contract tests cover pause, resume, cancel, and remote-stop uncertainty. |
| FR-016 | pass | Evidence Gate and unobserved blocking tests. |
| FR-017 | pass | Parent and Worker state-machine terminal guards. |
| FR-018 | pass | Usage records/export include latency and retry count; OpenAI-compatible adapter retries retryable failures and records the performed count while preserving unknown cost. Negative tests also verify provider error payloads, logs, and usage exports do not expose credential sentinels or private request bodies. |
| FR-019 | pass | The AgentScope Framework Adapter has ASGI and injected-runtime create/subscribe/pause/resume/cancel lifecycle coverage, plus Python 3.11 AgentScope 2.0.6 import verification. Credentialed remote-provider behavior remains adapter/environment evidence, not a local contract prerequisite. |
| FR-020 | pass | Historical SSE replay, Last-Event-ID continuation and explicit bounded `follow` polling for new journal events are implemented. |
| FR-021 | pass | Local副本适配与 Git detached worktree 均从同一基准创建 Owner workspace；规范工作树不作为 Worker 写入目标，真实临时 Git 仓库测试覆盖创建、基线和清理。 |
| FR-022 | pass | Local 与 Git 适配器都生成带基线、文件哈希和实际 Diff 的 ChangeSet，并在集成入口执行 Lease 范围校验；无法证明的区域仍 fail-closed。 |
| FR-023 | pass | Ordered atomic integration persists IntegrationAttempt transitions and can run through the journal-backed local IntegrationQueue with claim, ack, requeue and restart recovery. |
| FR-024 | pass | ParentVerification consumes durable integration facts and evidence; missing persisted integration results fail closed instead of accepting caller summaries. |
| FR-025 | pass | Parent/Child/Session/Attempt/Lease/Integration/Review/Verification/Approval models exist with public reads; Approval has monotonic versioning and expected-version guarded decisions. |
| FR-026 | pass | Strict request models, stable envelopes/error contracts, expected versions, and v1 request/response manifests are verified against Pydantic/OpenAPI. |
| FR-027 | pass | Provider timeout/retry, claim/lease heartbeat, cancellation grace, storage/disk guards, supervisor concurrency limits and kill-9 continuation evidence are covered. |
| FR-028 | pass | Tail quarantine, non-tail refusal, claim recovery and subprocess SIGKILL-equivalent tests. |
| FR-029 | pass | `create_app` accepts structural `StateStore`, `ArtifactStore`, and `CheckpointStore` adapters while retaining local defaults; the contract test proves the API writes through an injected recording state adapter and exposes all three injected objects. Queue/workspace injection remains separately supported. |
| FR-030 | pass | Approval persistence, monotonic version, expected-version conflict (409 without an event), decision idempotency, and restart replay tests. |
| FR-101 | pass | `classify_boundary` distinguishes new_task, material_amendment, continuation, clarification and control with host-declared requests winning at full confidence; rule-based keyword scoring otherwise; unparseable input falls back to low-confidence new_task so the host can override. |
| FR-102 | pass | `Profiler` reads only the `RoutingRequestEnvelope` and never executes or inspects controller state; it has no execution rights by construction. |
| FR-103 | pass | `RequirementGraph` outputs requirements, constraints and evidence gaps; `has_write_suggestion` is structurally always `False`, and advisory mode never creates WorkerPlans or write permissions. |
| FR-104 | pass | `AdvisoryEngine` returns suggestions only and never registers a controller, never creates a WorkerPlan, and never changes model/policy configuration; control/clarification and material-amendment requests fail closed. |
| FR-105 | pass | `RoutingCandidate` carries rejection codes, estimated input/output tokens, cost, latency, confidence and a stable basis version for both direct and worker candidates. |
| FR-106 | pass | `ShadowLedger` durably records unexecuted advice with a stable request fingerprint and `mark_compared` attaches the real outcome ref; records replay across restart and the `POST /v1/advisory` + `GET /v1/advisory/shadow` API is Idempotency-Key protected. |
| EVAL-001 | pass | Offline evaluation set `evaluation/frozen_tasks.json` freezes three task families (backend/API diagnosis, frontend browser verification, disjoint full-stack) with five tasks each, ground-truth delegation, and acceptance criteria; `scripts/evaluate_offline.py` runs advisory decisions plus live A/B/C measurements; archived raw evidence lives in `evaluation/evidence/`. First live run (2026-08-15, gpt-5-6-mini): 6/15 correct delegation, A/B/C pass 4/7/5 with cost ~$0.019 / $0.035 / $0.037. `quality_claim: false`; API keys are never written into output. |
| FR-203 | pass | `model-manifest.schema.json` now has independent `worker_status` and `controller_status` roles; `ModelProfile` (src/valueroute/routing/manifest.py) validates role fields, `ControllerRanker` (src/valueroute/routing/rank.py) selects only `controller_status == certified` and compatible candidates with a deterministic, role-specific rank. |
| FR-201 | pass | `OrchestrationMode.automatic` plus `AutomaticControllerService.ensure_controller` selects the first certified controller from candidate profiles and keeps it sticky across calls and journal replay; `POST /v1/controller-sessions/{session_id}/epochs/automatic` exposes it with Idempotency-Key protection. |
| FR-202 | pass | `AutomaticControllerService.switch_controller` refuses switching while session tasks are running (safe boundary), requires the current expected version, releases the previous epoch, and commits the new epoch and session in one journal frame (atomic); `POST /v1/controller-sessions/{session_id}/epochs/switch` is Idempotency-Key protected and replay-safe. |

The v0.0.2 advisory pipeline remains read-only: it is wired as an independent
`/v1/advisory` surface and must still demonstrate quality/cost/latency gains
against the v0.0.1 offline evaluation set before any automatic delegation is
enabled. Automatic *controller selection* (v0.1, FR-201/202) is separate from
automatic *worker delegation*: it picks the host controller from certified
candidates and does not auto-delegate task work.

The project is not release-ready while any P0 row remains partial. This is an audit artifact, not a performance or quality claim.
