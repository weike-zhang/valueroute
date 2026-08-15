# Evaluation

## Local coordination harness

The repository includes a repeatable local coordination harness:

```bash
python3 scripts/evaluate_v001.py --provider-id fixture --model-id deterministic-fixture --trials 3 --output /tmp/valueroute-evaluation.json
```

It freezes three task families—independent file changes, overlapping changes
that must serialize, and recovery after interruption—and preserves the code
fingerprint, runtime, raw trial results, and invariant outcomes. The harness
uses deterministic fixtures and therefore has `quality_claim: false`: it is
evidence for ValueRoute's coordination, workspace, integration, and replay
boundaries, not evidence that one model is better than another.

## Offline routing evaluation set (P1-7)

Design sections 18.2 and 20.2 require an offline evaluation set built from
frozen task families before any automatic routing delegation is enabled. The
set lives in `evaluation/`:

- `evaluation/frozen_tasks.json` — the frozen task set: three families
  (backend/API diagnosis and repair, frontend changes with real-browser
  verification, and disjoint full-stack changes), five tasks each, with
  per-task user text, write regions, ground-truth delegation, and acceptance
  criteria.
- `scripts/evaluate_offline.py` — the harness. For every task it builds a
  `RoutingRequestEnvelope`, runs the advisory pipeline, records the suggested
  delegation against ground truth, then measures a live model in three
  configurations (A: zero workers, B: one fixed worker, C: adaptive per the
  advisory result) and records tokens, wall time, and estimated cost.
- `evaluation/evidence/` — archived raw JSON output, one file per run, so any
  README performance claim can be traced back to configuration, tasks, and
  raw results.

Run it with a live OpenAI-compatible endpoint:

```bash
VALUEROUTE_EVAL_API_KEY=... python3 scripts/evaluate_offline.py \
  --base-url http://HOST:PORT/v1 --model gpt-5-6-mini \
  --output /tmp/valueroute-offline-eval.json
```

Or skip live calls to check only advisory routing decisions:

```bash
python3 scripts/evaluate_offline.py --skip-live --api-key dummy
```

The API key is read from `VALUEROUTE_EVAL_API_KEY` (or `--api-key`) and is
never written into the output JSON. The harness reports `quality_claim: false`
and an honest interpretation; acceptance is keyword-based rather than full
task execution, so the output is decision-and-cost evidence, not a
model-quality certification.

### First recorded run (2026-08-15, gpt-5-6-mini)

`evaluation/evidence/evaluation-2026-08-15-gpt-5-6-mini.json` records the
first live run:

- Advisory delegation vs ground truth: 6/15 correct, 5/15 over-delegated,
  4/15 under-delegated (40% / 33% / 27%).
- Config A (zero workers): 4/15 passed, 6,292 tokens, ~$0.0186.
- Config B (one fixed worker): 7/15 passed, 11,898 tokens, ~$0.0354.
- Config C (adaptive per advisory): 5/15 passed, 12,572 tokens, ~$0.0367.

Observed advisory limitation: the boundary classifier treats a bare `改`
(change) token as a material-amendment marker, so some disjoint full-stack
tasks that should delegate to two workers are conservatively classified as
amendments and not delegated. That is a real finding the offline set exists to
surface, not a claim that either the classifier or the model is wrong. A
public model-quality, cost, or latency comparison must additionally freeze
provider/model versions, configuration, region, repetition count, and price
table; no single live run is relabeled as such a claim.
