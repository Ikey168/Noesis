# Evidence-bundle v1 fixtures

- `valid-minimal.json` is a complete embedded receipt.
- `valid-external-reference.json` is internally valid but declares bytes the
  offline verifier deliberately does not fetch.
- `incomplete-declared-omission.json` truthfully declares missing content.
- `invalid-tampered-object.json` changes a payload without changing its digest.

The expected verifier statuses are enforced in
`tests/unit/evidence_bundle/test_bundle_core.py`.
