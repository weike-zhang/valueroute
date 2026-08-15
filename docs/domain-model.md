# Domain model

The durable aggregates are `ControllerSession`, `ControllerEpoch`, `ParentTask`, `WorkerAttempt`, `IntegrationAttempt`, `WriterLease`, `Approval`, `EvidenceRecord`, and `Checkpoint`. Pydantic strict models reject undeclared fields at API boundaries.

Parent completion requires completed child results, integrated ChangeSets, a passing Evidence Gate, and `ParentVerification`. `unobserved` is an evidence observation state, not a successful task state.

Retries create new WorkerAttempts. Integration retries create new IntegrationAttempts. Expected versions and terminal-state guards prevent last-write-wins transitions.
