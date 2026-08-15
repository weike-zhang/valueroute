# Evaluation

The repository now includes a repeatable local coordination harness:

```bash
python3 scripts/evaluate_v001.py --provider-id fixture --model-id deterministic-fixture --trials 3 --output /tmp/valueroute-evaluation.json
```

It freezes three task families—independent file changes, overlapping changes
that must serialize, and recovery after interruption—and preserves the code
fingerprint, runtime, raw trial results, and invariant outcomes. The harness
uses deterministic fixtures and therefore has `quality_claim: false`: it is
evidence for ValueRoute's coordination, workspace, integration, and replay
boundaries, not evidence that one model is better than another.

A public model-quality, cost, or latency comparison must replace the fixture
executor with a credentialed provider and additionally freeze provider/model
versions, configuration, region, task inputs, repetition count, price table,
and raw usage artifacts. The fixture output must not be relabeled as such a
claim.
