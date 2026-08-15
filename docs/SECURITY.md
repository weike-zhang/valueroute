# Security policy

ValueRoute is an early local-first Alpha candidate and is not a hardened multi-tenant service. Do not expose it to an untrusted network or place secrets in task goals, journal events, checkpoints, artifacts, workspace files, or bug reports.

## Reporting a vulnerability

Do not open a public issue for a suspected credential leak, workspace escape, authorization bypass, data exposure, or destructive recovery flaw. Until a private reporting channel is configured, contact the maintainer privately through the repository owner’s verified contact channel and include “ValueRoute security” in the subject. Minimize sensitive data and provide reproduction steps, affected commit, environment, and impact. Do not include API keys or personal data.

This repository does not currently publish a guaranteed response SLA, security bounty, or supported-version matrix.

## Current protections and boundaries

The implementation has local instance locking, path normalization, symlink rejection in local workspace snapshots, ChangeSet scope/base checks, structured artifact/checkpoint integrity hashes, storage/free-space limits, explicit unknown-cost accounting, and fail-closed evidence/ownership checks. The OpenAI adapter reads `OPENAI_API_KEY` from the environment and does not put it in domain models.

Negative regression tests cover the current public boundaries: provider HTTP error messages do not echo response bodies or credentials, provider failures do not enter captured logs, and usage exports contain neither secrets nor private task bodies. These tests do not make the local journal or generated artifacts safe for secrets; the security policy above still forbids putting sensitive data there.

Authentication, authorization, TLS, tenant isolation beyond request/idempotency fields, secret rotation, audit export, dependency/SBOM policy, and remote-host threat modeling are not implemented. Treat the local data directory and all generated artifacts as sensitive according to their task classification.
