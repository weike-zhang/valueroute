# Testing philosophy

Tests are contracts for invariants, not a checklist of superficial green paths. Assertions preserve meaningful failure states: conflicts, unobserved evidence, unsafe recovery, scope violations, and storage corruption must remain visible.

The suite is split into unit tests for domain/storage rules and contract tests for FastAPI persistence, idempotency, SSE, approvals, integration queries, restart behavior, and the versioned v1 request-schema publication contract. The schema contract test compares checked-in JSON Schema artifacts with their Pydantic source models and FastAPI OpenAPI components/routes. Fault tests use isolated temporary directories and subprocesses where process interruption matters.

Run:

```bash
python3 -m pytest -q
python3 -m compileall -q src
```
