# API specification

The versioned HTTP surface is under `/v1`.

- `GET /v1/health/live` and `GET /v1/health/ready`
- Controller session and epoch creation/query
- Parent task creation/query, planning, execution, pause, resume, cancel
- Child boundary, evidence, usage, approval, integration-attempt and integration-result queries
- Parent verification and resumable SSE events via `Last-Event-ID`

All write routes require `Idempotency-Key`; migrations use `expected_version`. Errors use stable `code` values in the response detail.

The v1 request publication contract is maintained in [`schemas/v1/manifest.json`](../schemas/v1/manifest.json). Each listed JSON file is generated from the named strict Pydantic request model in `valueroute.api.schemas`; the contract tests compare the checked-in artifact, the model JSON Schema, and the corresponding FastAPI OpenAPI component and route reference. Unknown request fields are rejected at the API boundary.

Stable response envelopes and route-to-component mappings are maintained in [`schemas/v1/response-manifest.json`](../schemas/v1/response-manifest.json) and checked against the generated OpenAPI document. Task queries use the `TaskView` projection; write responses retain the common `data`/`meta`/`error` envelope.
