# Architecture

ValueRoute is a local-first FastAPI service. The journal is the durable source of truth; in-memory indexes are rebuilt by replay. `ArtifactStore`, `CheckpointStore`, `WorkspaceAdapter`, and the execution queue are explicit boundaries so the core does not require a database or Redis.

The main flow is ControllerSession → ControllerEpoch → ParentTask → validated WorkerPlan → ChildTaskBoundary/OwnerAssignment → WorkerAttempt → journal-backed ExecutionQueue → bounded local ExecutionSupervisor → ChangeSet → ordered integration → ParentVerification.

The current process is single-instance per data directory. `LocalJournal` owns the instance lock and checksummed frames. Provider calls are made through injected adapters. Remote telemetry is not enabled by default.

Storage now writes generationed, checksummed replay snapshots under
`snapshots/` and atomically updates `snapshots/manifest.json`. Startup validates
the selected snapshot, falls back to an older valid generation, and then
replays only journal records newer than the snapshot sequence. `Store` exposes
`snapshot()`, `compact()`, and `rebuild()` through the local storage boundary.
`compact()` is intentionally a safe no-op for physical journal deletion: the
active journal is retained as the recovery source until immutable journal
segments and a retention policy exist. This avoids data loss if the newest
snapshot is later corrupted.

Every new `Store.commit_frame()` is encoded as one checksummed journal frame
with commit id, sequence range, expected versions, payload hash, and optional
idempotency result. Existing single-event callers use the compatibility
wrapper and retain the same replay behavior.

Known limits: automatic model routing, multi-process execution, and production authentication are not part of the current vertical slice. `GitWorkspaceAdapter.integrate()` deliberately refuses implicit writes; the host-owned `adopt_changeset()` boundary creates a commit in a disposable integration worktree without moving the canonical checkout. The `valueroute` entrypoint enables the HTTP-owned Supervisor when `VALUEROUTE_OPENAI_MODEL_ID` is configured; tests and embedded hosts can inject any Provider adapter directly.
