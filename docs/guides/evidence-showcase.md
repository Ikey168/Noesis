# Evidence showcase

Run one offline command from a fresh checkout:

```bash
python scripts/evidence_showcase.py --output /tmp/noesis-receipts.json
```

The deterministic bundled flow invokes the same implementation used by MCP
and REST consumers:

1. `kb_search` finds the seeded documents;
2. `kb_claims` returns the cited presentation-time claim clusters;
3. `corroborate` counts a genuinely independent supporting source;
4. `claim_vs_data` refuses to guess when no statistical series matches;
5. `kb_brief` renders the bounded, cited result.

The receipt deliberately includes three negative cases rather than hiding
them: an uncited locator with `cited: false`, an `unverifiable` quantitative
claim, and `person_requires_documents` for a person absent from the public
corpus. The final `verification` object checks the honesty envelope with
`validate_analytic_output()`, citation coverage, corroboration, and every
expected refusal state. A failed check makes the command exit non-zero.

No network, model download, API key, Airflow, or Docker service is involved.
