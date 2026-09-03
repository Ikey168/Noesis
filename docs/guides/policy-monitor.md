# Policy monitor tutorial

This tutorial runs a complete policy-change workflow locally: connector
ingestion, domain provisioning, revision classification, reporting-origin
analysis, a quantitative filing and vote, an authorized stale-guidance alert,
a cited brief, and a verifiable evidence bundle. The Clean Heat rule and every
organization, URL, vote, memo, and number in the fixture are fictional. The
fixture is synthetic, CC0-1.0 licensed, and safe to redistribute.

## Run the scenario

From the repository root:

```bash
make policy-monitor
```

No service or network connection is required. The command is idempotent; a
second run against the same database produces the same logical watch event and
bundle identity. To put the artifacts elsewhere:

```bash
python scripts/policy_monitor_demo.py \
  --output /tmp/noesis-policy-monitor \
  --database /tmp/noesis-policy-monitor.duckdb
```

The summary should report nine public documents, four supporting publications
that resolve to two probable reporting origins, one contradiction, one vote,
`private_guidance_status: stale`, a matching replay, and a valid bundle.

## Inspect the receipts

```bash
jq '.metrics' artifacts/policy-monitor/public-answer.json
jq '.private_guidance' artifacts/policy-monitor/authorized-answer.json
jq '.poll.events[] | {event_type, reason_code, watermark}' \
  artifacts/policy-monitor/watch.json
cat artifacts/policy-monitor/brief.md
```

The public answer is deliberately silent about whether any private corpus
exists. The authorized answer is a separate artifact produced only after the
demo grants its fixed operator access to `clean-heat-private`.

## Architecture and trust guarantees

The workflow composes the registered manifest, legislative, filings, and
upload connectors with the canonical document store. Domain membership is
materialized by the normal KB provisioning pass, and public reads resolve the
`clean-heat-public` `DomainBacking` rather than querying fixture tables.
Revisions and snapshots feed a reusable versioned-assertion comparison; the
evidence-independence graph keeps publication count distinct from probable
origin count. The standard watch store supplies authorization, committed
watermarks, immutable event IDs, and deterministic replay. Finally, the
standard evidence-bundle exporter hashes the cited receipt and the offline
verifier checks every object and reference.

These guarantees do not establish that the fictional facts are true. They
establish which records support each line, which values changed, which origin
relationships are inferred, what was visible to the caller, and whether the
portable artifact was mutated.

Verify the exported public bundle:

```bash
python scripts/evidence_bundle.py verify \
  artifacts/policy-monitor/evidence-bundle.json
```

Mutation is detected. For example, copy the bundle, alter any embedded payload
value, and run the verifier again; it exits non-zero with a digest mismatch.

## Use the Python, REST, and MCP surfaces

The direct Python boundary is:

```python
from src.policy_monitor import public_view, authorized_view

public_receipt = public_view(conn)
private_receipt = authorized_view(conn, principal_id="policy-monitor-operator")
```

The REST mirror exposes:

```text
GET /api/v1/kb/policy-monitor
GET /api/v1/kb/policy-monitor/bundle
GET /api/v1/kb/policy-monitor/private
GET /api/v1/kb/policy-monitor/private/bundle
```

The private routes require normal API authentication and a matching stored
domain grant. MCP clients use `policy_monitor_status` and
`policy_monitor_bundle`; `include_private=true` likewise requires both a
principal ID and its grant.

## Replace the fixture

`scripts/policy_monitor_demo.py --fixture ... --domains ...` accepts another
local manifest with the same fixture contract. The loader rejects manifests
that are not explicitly marked `synthetic: true` and `license: CC0-1.0`. For a
real deployment, provision licensed records through the corresponding source
connectors and retain the same public/private domain split.

[`examples/policy-monitor/live.example.yml`](../../examples/policy-monitor/live.example.yml)
is a disabled, opt-in live template. It documents the required endpoint or
path variables, permission expectations, and per-source request ceilings. It
must be reviewed and changed to `enabled: true` by an operator; the offline
command never reads it.

Receipt semantics and the privacy guarantees are specified in
[`contracts/noesis-policy-monitor-v1.md`](../../contracts/noesis-policy-monitor-v1.md).
