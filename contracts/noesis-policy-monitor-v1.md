# Noesis policy monitor v1

`noesis-policy-monitor-v1` is the canonical receipt for comparing versioned
public policy records with optional, explicitly authorized private guidance.
The bundled Clean Heat scenario is fictional, synthetic, CC0-licensed, and
network-free.

## Operations

- Python: `public_view`, `authorized_view`, and `export_policy_bundle` in
  `src.policy_monitor`.
- MCP: `policy_monitor_status` and `policy_monitor_bundle`.
- REST: `GET /api/v1/kb/policy-monitor` and
  `GET /api/v1/kb/policy-monitor/bundle`; the `/private` variants require the
  authenticated principal to hold the private-domain grant.
- Demo: `make policy-monitor` provisions the scenario, answers it, renders a
  cited brief, emits and replays the watch transition, exports the default
  public bundle, and verifies it.

## Privacy boundary

The public receipt is computed only from `clean-heat-public` membership. It
contains no private IDs, counts, status fields, redaction markers, or existence
signals. Setting `include_private=true` is insufficient by itself: a principal
must also have an explicit stored grant for `clean-heat-private`. Authorized
comparisons and exports are audited without copying memo text into the audit
log. Evidence bundles remain public-only unless this same authorization check
passes.

## Determinism and honesty

All fixture timestamps and committed watch watermarks are fixed. The
`guidance_stale` event is derived from the transition between retained
snapshots and uses the standard watch idempotency key. Replay must match the
stored logical event. Every factual statement has one or more evidence
locators; model predictions and source records are labeled separately. The
JSON Schema is
`contracts/schemas/jsonschema/noesis-policy-monitor-v1.json`.

The revision classifier, document store, domain membership pass, quantitative
observation store, evidence-independence graph, watch store, and evidence
bundle verifier are production components. The workflow does not maintain a
parallel demo-only truth table.
