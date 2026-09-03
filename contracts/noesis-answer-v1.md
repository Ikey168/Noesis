# noesis-answer-v1 — verifiable question answering

`noesis-answer-v1` is the payload returned in the standard `noesis-kb-v1`
envelope by `kb_answer`. It is an additive operation: no existing KB response
shape changes, and a future breaking answer change requires a new nested answer
contract version.

The governed JSON Schema is
`contracts/schemas/jsonschema/noesis-answer-v1.json`. The contract MCP resolves
it as `noesis-answer-v1`, `kb-answer`, or `verifiable-answer`. The legacy
`answer` alias continues to identify `ask-response-v1` for compatibility.

## Truth boundary

The deterministic path extracts existing claims or document titles. It does
not generate factual prose and does not claim that cited material is true.
A citation proves where text appeared. Verdicts describe the evidence currently
available to Noesis:

- `supported`: cited material supports or contains the selected statement;
- `contradicted`: cited conflicting evidence or a quantitative check disagrees;
- `unverifiable`: the available evidence cannot support a factual answer.

Supporting and contradicting evidence are always separate arrays. A factual
statement must have a resolved locator (`cited: true`) or be visibly marked
`unverifiable`. The rendered form repeats that state; it may not hide an
uncited statement.

## Deterministic evidence plan

The offline engine tokenizes the question, removes a fixed stop-word set,
scores overlap against claim text, and uses a documented minimum threshold.
Ties are ordered by stable cluster or document identifier. If no claim passes,
document titles are considered. If no candidate passes, the response contains
one explicit, uncited `unverifiable` statement and an
`insufficient_evidence` refusal.

The response records the method, assumptions, threshold, normalized question
tokens, number of candidates considered, selected identifiers, and relevance
scores. Statement identifiers are SHA-256-derived from the normalized question
and selected evidence identity, so identical inputs produce identical IDs.

## Evidence and uncertainty

Each statement contains:

- stable `id`, exact extractive `text`, and three-valued `verdict`;
- `supporting_evidence[]` and `contradicting_evidence[]` locators;
- publication, distinct-source, and unresolved-source counts;
- the active independence method and assumptions;
- `prediction_mode`, model confidence when retained by the backing, and an
  interval when a stored quantitative check provides one;
- `confidence_scope`, which labels retained scores as claim-extraction model
  confidence rather than answer confidence;
- the complete quantitative honesty envelope where applicable;
- integrity status and evidence-backed findings for cited documents;
- statement-specific method and assumptions.

There is deliberately no overall confidence score. Extraction-model scores,
source counts, relevance, integrity findings, and quantitative intervals are
heterogeneous observations and must not be collapsed into one misleading
number.

Distinct source identity is the v1 independence method. It does not infer
syndication or shared reporting origin; that limitation is explicit so the
Evidence Independence Graph can replace the method without changing the rest
of the response shape.

## Surfaces

- Python: `src.kb.contract.kb_answer(domain, question, ...)`
- MCP: `kb_answer(domain, question, ...)`
- REST: `GET /api/v1/kb/{domain}/answer?q=...`

All three call the same implementation. `limit` is 1–20,
`minimum_relevance` is 0–1, and questions are non-empty with a 5,000-character
maximum. Contract errors retain the existing KB error mappings.

Working examples:

```python
from src.kb.contract import kb_answer

response = kb_answer("economics", "What was annual inflation in 2025?")
```

```json
{
  "name": "kb_answer",
  "arguments": {
    "domain": "economics",
    "question": "What was annual inflation in 2025?",
    "limit": 5,
    "minimum_relevance": 0.34
  }
}
```

```console
curl --get http://localhost:8000/api/v1/kb/economics/answer \
  --data-urlencode 'q=What was annual inflation in 2025?' \
  --data 'limit=5' \
  --data 'minimum_relevance=0.34'
```

## Optional synthesis

An LLM renderer may be added later only if it consumes this evidence plan and
cannot introduce statements absent from it. The canonical offline response
does not require a network, API key, model download, Docker, or LLM.
