# ValueRoute

ValueRoute is an independent FastAPI service for bounded model and worker orchestration.

The implementation follows [ValueRoute-详细设计与需求规格.md](ValueRoute-详细设计与需求规格.md). The current v0.0.1 work is being built as a correctness-first vertical slice:

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

## Development

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
VALUEROUTE_DATA_DIR=/tmp/valueroute-data python3 -m valueroute.main
```

The service exposes `/v1/health/live`, `/v1/health/ready`, session/task APIs, and a journal-backed SSE event endpoint. Database, Redis, and remote state services are intentionally not required by v0.0.1.

Design and operation notes:

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [API specification](docs/api-spec.md)
- [Versioned API schemas](schemas/v1/manifest.json)
- [Ownership and leases](docs/ownership-and-region-lease.md)
- [Checkpoint and recovery](docs/checkpoint-and-recovery.md)
- [Testing philosophy](docs/testing-philosophy.md)
- [Evaluation](docs/evaluation.md)
- [AgentScope host example](docs/agentscope-example.md)
- [v0.0.1 acceptance matrix](docs/acceptance-matrix.md)

The v0.0.1 local coordination path is implemented and covered by the
acceptance matrix. Real credentialed provider/model-quality evaluation,
production authentication and remote deployment hardening remain explicitly
outside this local-first release evidence.

## Documentation

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [API specification](docs/api-spec.md)
- [Ownership and region leases](docs/ownership-and-region-lease.md)
- [Checkpoint and recovery](docs/checkpoint-and-recovery.md)
- [Testing philosophy](docs/testing-philosophy.md)
- [Evaluation](docs/evaluation.md)
- [Security](docs/SECURITY.md)
- [Contributing](docs/CONTRIBUTING.md)

These documents describe the checked-in v0.0.1 implementation. They intentionally label gaps and do not claim throughput, latency, reliability, or model-quality results that have not been measured.
