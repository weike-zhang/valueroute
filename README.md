# ValueRoute

ValueRoute is an independent FastAPI service for bounded model and worker orchestration.

The implementation follows [ValueRoute-详细设计与需求规格.md](ValueRoute-详细设计与需求规格.md). The current v0.0.2 work is built as a correctness-first vertical slice:

- local append-only JSONL journal;
- explicit ControllerSession and ControllerEpoch registration;
- ParentTask and deterministic WorkerPlan validation;
- expected-version and Idempotency-Key protection;
- explicit execute, pause, resume and cancel transitions;
- conservative ResourceRegion overlap checks.
- content-addressed Artifacts, structured Checkpoints, and checksummed journal recovery;
- isolated local workspaces, ChangeSet validation, ordered atomic integration, and parent verification;
- AgentScope lifecycle/SSE bridge and provider-boundary Worker recovery checkpoints;
- bounded local `ExecutionSupervisor` over the journal-backed queue;
- versioned v1 request JSON Schema artifacts checked against Pydantic and OpenAPI;
- configurable storage, disk, claim, lease, and provider runtime protections.
- read-only v0.0.2 advisory routing: request-boundary classification, a
  `RequirementGraph` profiler, conservative candidate suggestions with
  cost/latency estimates, and durable shadow records for offline comparison —
  all exposed via the Idempotency-Key-protected `/v1/advisory` API.

## Development

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
VALUEROUTE_DATA_DIR=/tmp/valueroute-data python3 -m valueroute.main
```

The service exposes `/v1/health/live`, `/v1/health/ready`, session/task APIs, and a journal-backed SSE event endpoint. Database, Redis, and remote state services are intentionally not required by v0.0.2.

Design and operation notes:

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [API specification](docs/api-spec.md)
- [Versioned API schemas](schemas/v1/manifest.json)
- [Model profiles](model_manifests/README.md)
- [NFR and supply-chain evidence](docs/nfr-and-supply-chain-evidence.md)
- [Ownership and leases](docs/ownership-and-region-lease.md)
- [Checkpoint and recovery](docs/checkpoint-and-recovery.md)
- [Testing philosophy](docs/testing-philosophy.md)
- [Evaluation](docs/evaluation.md)
- [AgentScope host example](docs/agentscope-example.md)
- [v0.0.2 acceptance matrix](docs/acceptance-matrix.md)

The v0.0.1 and v0.0.2 implementation paths are covered by the
acceptance matrix. Real credentialed provider/model-quality evaluation,
production authentication and remote deployment hardening remain explicitly
outside this local-first release evidence.

The v0.0.2 advisory routing pipeline (boundary classification, profiler,
candidate suggestions, and durable shadow records) is implemented as a
read-only surface: it only recommends and never modifies the Controller,
WorkerPlan, or model configuration. Automatic delegation stays disabled until
the offline evaluation set demonstrates quality, cost, or latency gains.

## Documentation

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [API specification](docs/api-spec.md)
- [Ownership and region leases](docs/ownership-and-region-lease.md)
- [Checkpoint and recovery](docs/checkpoint-and-recovery.md)
- [Testing philosophy](docs/testing-philosophy.md)
- [Evaluation](docs/evaluation.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

These documents describe the checked-in v0.0.2 implementation. They intentionally label gaps and do not claim throughput, latency, reliability, or model-quality results that have not been measured.

## Repository

- Home: <https://github.com/weike-zhang/valueroute>
- Changelog: [CHANGELOG.md](CHANGELOG.md)
- License: [LICENSE](LICENSE)
