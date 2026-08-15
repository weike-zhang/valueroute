# Testing philosophy

Tests are contracts for invariants, not a checklist of superficial green paths. Assertions preserve meaningful failure states: conflicts, unobserved evidence, unsafe recovery, scope violations, and storage corruption must remain visible.

The suite is split into unit tests for domain/storage rules and contract tests for FastAPI persistence, idempotency, SSE, approvals, integration queries, restart behavior, and the versioned v1 request-schema publication contract. The schema contract test compares checked-in JSON Schema artifacts with their Pydantic source models and FastAPI OpenAPI components/routes. Fault tests use isolated temporary directories and subprocesses where process interruption matters.

## Coverage against design section 20.1

| Layer | Where covered |
|---|---|
| 1. Schema | `tests/contract/test_versioned_api_schema.py`, `test_versioned_response_schema.py`, `test_api_schema.py`; event frame/Last-Event-ID in `tests/unit/test_events.py` |
| 2. State machine | `tests/unit/test_state_machine.py` (legal/illegal transitions, terminal immutability, terminal states stay immutable after journal replay) |
| 3. Commit | version conflicts, leases, duplicate attempts, Idempotency-Key replay across `test_approvals.py`, `test_claims.py`, `test_lease_manager.py`, `test_execution_queue.py`, `test_control.py`, `test_api.py`, `test_api_schema.py` |
| 4. Region | `tests/unit/test_lease_overlap.py`, `test_region_resolver.py` (symbols, path prefixes, key ranges, subresources) |
| 5. Fault injection | journal tail quarantine and corruption in `test_local_storage.py`; SIGKILL recovery in `test_execution_claim_integration.py`; provider timeout/cancel and timeout-cancel race in `test_runner.py`; disk-free threshold in `test_runtime_protection.py` |
| 6. Adapter contracts | `tests/unit/test_adapters.py`, `tests/contract/test_agentscope_e2e.py`, `test_review_verifier.py` |
| 7. Real-path | real file/git worktree changes in `test_workspaces.py`, `test_git_workspaces.py`; restart/replay across `test_local_storage.py`, `test_supervisor.py`, `test_integration_service.py` |
| 8. Isolation & integration | owner scope violations, base drift, merge conflict, and rollback (canonical stays at the last committed revision on failure) in `test_workspaces.py`, `test_integration_service.py`, `test_parent_verification.py` |

Run:

```bash
python3 -m pytest -q
python3 -m compileall -q src
```
