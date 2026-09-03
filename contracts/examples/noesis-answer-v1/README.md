# Noesis Answer v1 examples

- `valid-supported.json` demonstrates a cited extractive statement.
- `valid-refusal.json` demonstrates an explicit insufficient-evidence refusal.
- `invalid-uncited-supported.json` is intentionally rejected because it marks
  a factual statement supported without any cited evidence.
- `evaluation-cases.json` is the committed six-scenario golden set used to
  measure citation coverage, evidence precision, abstention correctness, and
  deterministic stability across support, contradiction, refusal,
  quantitative, private-domain, and integrity behavior.

Validate either through the contract MCP with `kb-answer` or directly with
the `noesis-answer-v1` JSON Schema.
