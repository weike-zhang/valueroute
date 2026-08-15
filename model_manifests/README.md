# Model manifests

This directory holds versioned, role-specific model profiles as defined in
design section 15.1. A profile records what was measured for one `provider_id`
+ `model_id` combination in one region at one point in time, with references to
the evidence that produced it.

The v0.1 boundary: **Worker** and **Controller** roles are certified
independently. `Profiler` ranking is certified separately later; the roles must
never be replaced by a single aggregate ranking.

## Files

- `model-manifest.schema.json` — the versioned `v1` JSON Schema for a
  `ModelProfile` manifest, matching the design's `model_profile` fields,
  including independent `worker_status` and `controller_status`.
- `openai-worker.example.json` — a documented, non-authoritative example
  profile. It is not measurement evidence; `evidence_refs` must point at real
  artifacts before any production claim is made.

## Rules

- Never hard-code a model ID in the design or code; model selection reads
  versioned manifests from this directory.
- A `worker_status` or `controller_status` of `certified` requires
  reproducible measurement evidence. Until that evidence exists, the status
  must stay `candidate`.
- Automatic controller selection (`automatic` mode, FR-201) only considers
  `controller_status == certified` and `protocol_status == compatible`
  profiles; see `src/valueroute/routing/rank.py`.
- Profile updates record `measured_at`, model version, configuration, region,
  and raw usage artifacts; a profile without a reproducible measurement must
  not be relabeled as certified.
