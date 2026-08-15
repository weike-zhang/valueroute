# Contributing

Contributions should preserve the narrow v0.0.2 correctness boundary. Before changing behavior, read the relevant design section and the linked implementation modules. Keep domain rules deterministic and make failure states explicit.

## Local setup

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
VALUEROUTE_DATA_DIR=/tmp/valueroute-data python3 -m valueroute.main
```

Do not commit credentials, local data directories, generated artifacts, checkpoints, journal contents, or private workspace files. Use temporary directories in tests.

## Change expectations

- Add or update focused unit/contract tests for changed boundaries.
- Preserve expected-version and idempotency behavior for writes.
- Keep recovery facts separate from provider private state; never claim automatic resume unless implemented and tested.
- Treat unknown cost as unknown, not zero.
- For workspace changes, test path escape, symlink, scope, base-revision, conflict, and atomic integration behavior.
- For advisory routing changes, keep the pipeline read-only: suggestions must never register a controller, create a WorkerPlan, or change model/policy configuration. Shadow records are offline evidence only and grant no execution rights.
- Document externally visible behavior and explicitly list unfinished work.
- Do not add performance conclusions without a reproducible benchmark and environment.

## Scope and review

This repository currently has no declared contributor covenant, release process, SBOM publication, dependency lock policy, or automated formatting/lint gate. Reviewers should therefore inspect the diff, run the focused tests and full available suite, and report unrelated failures separately. Changes to implementation that alter the architecture or limits should update the corresponding document and the detailed design/implementation log when those artifacts are introduced.
