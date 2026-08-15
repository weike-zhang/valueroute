# Model manifests

This directory holds versioned, role-specific model profiles as defined in
design section 15.1. A profile records what was measured for one `provider_id`
+ `model_id` combination in one region at one point in time, with references to
the evidence that produced it.

The v0.0.2 boundary: only the **Worker** role certification is required.
`Profiler` and `Controller` ranking are certified separately in v0.1 and must
never be replaced by a single aggregate ranking.

## Files

- `model-manifest.schema.json` — the versioned `v1` JSON Schema for a
  `ModelProfile` manifest, matching the design's `model_profile` fields.
- `openai-worker.example.json` — a documented, non-authoritative example
  profile. It is not measurement evidence; `evidence_refs` must point at real
  artifacts before any production claim is made.

## Rules

- Never hard-code a model ID in the design or code; model selection reads
  versioned manifests from this directory.
- A `worker_status` of `certified` requires reproducible measurement evidence.
  Until that evidence exists, the status must stay `candidate`.
- Profile updates record `measured_at`, model version, configuration, region,
  and raw usage artifacts; a profile without a reproducible measurement must
  not be relabeled as certified.
