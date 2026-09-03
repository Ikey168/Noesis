# Evidence showcase

Run one offline command from a fresh checkout:

```bash
python scripts/evidence_showcase.py --output /tmp/noesis-receipts.json
```

To emit the same run as a portable content-addressed evidence package and
verify it offline:

```bash
python scripts/evidence_showcase.py \
  --output /tmp/noesis-receipts.json \
  --bundle-output /tmp/noesis-evidence.json \
  --answer-bundle-output /tmp/noesis-answer-evidence.json
python -m src.evidence_bundle verify /tmp/noesis-evidence.json --json
python -m src.evidence_bundle verify /tmp/noesis-answer-evidence.json --json
```

The deterministic bundled flow invokes the same implementation used by MCP
and REST consumers:

1. `kb_search` finds the seeded documents;
2. `kb_claims` returns the cited presentation-time claim clusters;
3. `corroborate` counts a genuinely independent supporting source;
4. `claim_vs_data` refuses to guess when no statistical series matches;
5. `kb_answer` produces a deterministic, statement-level answer and an
   explicit insufficient-evidence refusal;
6. `kb_brief` renders the bounded, cited result;
7. the complete Answer v1 response is exported and verified as a portable
   evidence bundle.

The receipt deliberately includes three negative cases rather than hiding
them: an uncited locator with `cited: false`, an `unverifiable` quantitative
claim, and `person_requires_documents` for a person absent from the public
corpus. It also includes an `insufficient_evidence` Answer v1 refusal for a
question with no matching evidence. The final `verification` object checks the honesty envelope with
`validate_analytic_output()`, citation coverage, corroboration, and every
expected refusal state. A failed check makes the command exit non-zero.

No network, model download, API key, Airflow, or Docker service is involved.
The bundle verifier additionally checks schema validity, canonical SHA-256
object hashes, bundle-local reference closure, citation requirements, and
statistical-honesty envelopes. It deliberately does not claim that a cited
source is true. See the
[`noesis-evidence-bundle-v1` contract](../../contracts/noesis-evidence-bundle-v1.md).
The structured answer semantics are specified by the
[`noesis-answer-v1` contract](../../contracts/noesis-answer-v1.md).
