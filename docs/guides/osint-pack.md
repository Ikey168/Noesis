# OSINT pack guide

The OSINT pack (`packs/osint/`, `src/osint/`, `tools/osint_mcp/server.py`)
analyses documents and registry facts the operator already collected. Every
line is cited, no person is targeted, and anything pointed at the open web
sits behind the review gate (`docs/security/osint-review-gate.md`). This guide
covers the expansion tracked in #2040.

## Separated reliability and credibility (OX01)

`source_reliability(source)` adds `reliability_grade`: an Admiralty letter for
the **source**.

| Letter | Meaning | Rule (score = mean of transparency, corroboration hit-rate, clean-record rate) |
| --- | --- | --- |
| A | completely reliable | score ≥ 0.85 and a track record of at least 20 |
| B | usually reliable | score ≥ 0.70 (A capped to B below 20) |
| C | fairly reliable | score ≥ 0.55 |
| D | not usually reliable | score ≥ 0.40 |
| E | unreliable | below 0.40 |
| F | cannot be judged | track record below 5 documents/claims, whatever the score |

`corroborate(claim_id)` adds `credibility_grade`, a digit for the
**information**, next to the carrying source's `source_reliability_grade`:

| Digit | Meaning | Rule |
| --- | --- | --- |
| 1 | confirmed | ≥ 2 independent supporting origins, none contradicting |
| 2 | probably true | ≥ 2 supporting origins, weighted support ≥ 2 × weighted contradiction |
| 3 | possibly true | one uncontradicted origin, or support ≥ contradiction |
| 4 | doubtful | weighted contradiction > weighted support |
| 5 | improbable | only contradicting origins, or single-sourced and fact-checked as disputed |
| 6 | cannot be judged | single-sourced, or lineage unresolved |

Both blocks carry `derivation`, `method` and `assumptions`. They are
independent axes (`grading.independent_axes: true`); nothing fuses them into
one number. The contract is `noesis-osint-corroboration` 1.1.0 (additive).

## Ownership in the entity dossier (OX02)

`entity_dossier(entity, ownership_namespace=...)` adds an `ownership` section
for an organization when the Corporate Ownership bundle is enabled in that
namespace (optional feature `ownership`, off by default). The entity is
resolved to ownership records only through an exact ownership record key,
the ownership canonical entity id, or an accepted, unreverted identity
decision; names are never matched. Ambiguous resolution returns candidates.
Lines (identity with LEI/register numbers, direct and ultimate parents,
subsidiaries, officers, reporting exceptions) each cite the ownership record
revision, provider, jurisdiction and validity; conflicting assertions are
listed side by side. The section is never assembled for a person entity.

Per-list sanctions designations (OX03) wait for the Legal sanctions feature
(#1907); no `designations` feature is declared yet.

## Recycled video frames (OX04)

Pass an `ImageAssetStore` to `MediaConnector(asset_store=...)` and every
sampled keyframe becomes a corpus image asset (parent = the media document)
with an appearance whose `context` records the offset and scene index. dHash
is computed as for stills; EXIF is recorded empty. `image_reuse_findings`
labels appearances `video_frame` or `still_image` and cites frames by document
and offset; a video counts as one document. Without ffmpeg nothing is written.

## Registry and certificate history (OX05, OX06)

```python
from src.ingestion import rdap, crtsh

receipt = rdap.acquire_rdap(conn, "example.org", request_id="vet-1")
rdap.project_rdap(conn, "osint", receipt["observation_id"], principal_id=..., scopes=...)
receipt = crtsh.acquire_crtsh(conn, "example.org", request_id="vet-2")
crtsh.project_crtsh(conn, "osint", receipt["observation_id"], principal_id=..., scopes=...)
```

One domain per call (wildcards, IP literals and e-mail addresses are
refused), one request, no retries, a durable receipt keyed by `request_id`.
Results are `osint-observation-v1` records with archive-time semantics.
Natural-person registrant data and e-mail SANs are dropped before storage.
Projection appends `source_identity` revisions (the registration and issuance
history, each citing its observation), an `ownership` relation from the
registrant organization, and a `shared-infrastructure` relation (`status:
probable`, CDN caveat) when a certificate covers another source's domain.
`acquire_for_source` limits acquisition to a source identity's own domains;
`trace_artifact(document_id="osint-obs:...")` shows the chain, and passing
`investigation=` logs the receipt into `investigation_audit`.

Passive DNS: no provider adopted (`docs/security/osint-passive-dns-access.md`).

## Infrastructure pivot (OX08)

`infrastructure_pivot(identifier)` takes a domain or a source-identity id and
walks `ownership` and `shared-infrastructure` relations (depth 1–3), each hop
with its observation citations and status. It refuses e-mail addresses,
`@handles`, usernames, IP addresses and `person:` ids
(`person_identifier_refused`). There is no same-operator verdict.

## Gated imagery tier (OX09–OX11)

With `NOESIS_OSINT_GATED_TOOLS=on`:

- `chronolocate_image` proposes a capture-time band and season window for a
  corpus image, for a confirmed geolocation suggestion's place or an explicit
  operator hypothesis, from an injected shadow estimator (none ships) and
  local solar geometry. Queued `cited: false`, `verified: false`.
- `reference_imagery` attaches one satellite or street-level reference to a
  queued suggestion, through an allowlisted, budgeted provider (none ships).
  Candidate adapter: the Sentinel Hub Process API for satellite tiles
  (OAuth client credentials supplied by the deployment; its terms and
  attribution requirements must be verified before use). References are
  never citations; `confirm_suggestion(..., viewed_references=[...])` records
  which ones the operator compared.
- Reverse image search: see "Deploying a reverse-image provider" in the
  review gate and the conformance harness `tests/unit/osint/provider_conformance.py`.

## Workflows

- `osint.location-investigation`: place a reported event against a boundary.
- `osint.source-vetting`: acquire registry facts, read the reliability card,
  pivot over infrastructure relations, record the finding.
