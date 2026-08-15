# Contributing

Install development dependencies and run the full test suite before submitting changes:

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

Keep domain transitions explicit, preserve stable error codes, add a focused regression test for every invariant, and report unrelated baseline failures separately. Do not weaken assertions or silently turn unknown evidence into success.
